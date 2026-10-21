"""Turn a source file into plain text, on this machine.

- PDF: PyMuPDF text layer. A page with (almost) no text is rendered and OCR'd.
  Large images on a text page are OCR'd too, so text inside an embedded
  screenshot isn't lost. Files attached to the PDF are read as well: a PDF
  Portfolio is just a cover page plus these, and a page can carry a paperclip
  attachment.
- PNG/JPG/WEBP/BMP/TIFF/GIF: RapidOCR (ONNX models bundled in the package).
- TXT/MD: read as text. EML: headers and body; attachments are named, not opened.
- DOCX: paragraphs, tables, headers and footers (python-docx, if installed).

Only text comes out. Images are never copied anywhere.
"""

from __future__ import annotations

from redact_local import netguard

netguard.install()

import email  # noqa: E402
import io  # noqa: E402
import logging  # noqa: E402
import re  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from email import policy  # noqa: E402
from html.parser import HTMLParser  # noqa: E402
from pathlib import Path  # noqa: E402

from redact_local.errors import SafeError  # noqa: E402
from redact_local.textnorm import normalize_text  # noqa: E402

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
TEXT_EXT = {".txt", ".md", ".markdown"}
SUPPORTED = IMAGE_EXT | TEXT_EXT | {".pdf", ".eml", ".docx"}

MIN_TEXT_CHARS = 25        # fewer characters than this on a PDF page -> OCR it
PAGE_DPI = 300
REGION_DPI = 200
REGION_MIN_SHARE = 0.10    # OCR embedded images covering >= 10% of a text page
MAX_ATTACHMENT_DEPTH = 3   # a portfolio inside a portfolio inside a portfolio
MAX_ATTACHMENTS = 1000     # per file on disk, all levels: nested copies can multiply from a few KB
SNIFF_MAX_BYTES = 64 * 2**20   # an attached file with no usable extension is only unpacked to check its type below this

log = logging.getLogger("redact_local.extract")  # the pipeline sends this to the private run.log


@dataclass
class Extracted:
    text: str
    kind: str
    pages: int = 0
    text_pages: int = 0
    ocr_pages: int = 0
    ocr_regions: int = 0
    images_ocr: int = 0
    attachments: int = 0           # files attached to a PDF that were read
    attachments_skipped: int = 0   # not opened: an unsupported type, or an email's attachment
    attachments_failed: int = 0    # attached to a PDF but couldn't be read: the run is partial
    notes: list[str] = field(default_factory=list)  # private: shown in review.html only


def _read(src: Path | bytes) -> bytes:
    """A file on disk, or the bytes of a file embedded in a PDF."""
    return src.read_bytes() if isinstance(src, Path) else src


# ── OCR ────────────────────────────────────────────────────────────────────

_OCR = None


def _ocr_engine():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR = RapidOCR()
    return _OCR


def ocr_array(rgb) -> str:
    """OCR an RGB numpy array; return lines in reading order."""
    import numpy as np

    bgr = np.ascontiguousarray(rgb[:, :, ::-1])
    result, _ = _ocr_engine()(bgr)
    if not result:
        return ""
    items = []
    for box, text, _score in result:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        items.append((min(ys), max(ys), min(xs), text))
    items.sort(key=lambda t: ((t[0] + t[1]) / 2, t[2]))
    lines: list[dict] = []
    for top, bottom, left, text in items:
        mid, height = (top + bottom) / 2, max(1.0, bottom - top)
        if lines and abs(mid - lines[-1]["mid"]) <= max(4.0, 0.45 * height):
            lines[-1]["items"].append((left, text))
        else:
            lines.append({"mid": mid, "items": [(left, text)]})
    return "\n".join(" ".join(t for _, t in sorted(line["items"])) for line in lines)


def _pil_to_rgb(img):
    """RGB array; transparent areas become white (black would hide dark text from OCR)."""
    import numpy as np
    from PIL import Image

    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(background, rgba)
    return np.array(img.convert("RGB"))


def _pixmap_to_rgb(pix):
    import numpy as np

    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 1:
        arr = np.repeat(arr, 3, axis=2)
    return arr[:, :, :3]


# ── per type ───────────────────────────────────────────────────────────────

def _image(src: Path | bytes) -> Extracted:
    from PIL import Image, ImageSequence, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(_read(src)))
    except (UnidentifiedImageError, OSError):
        raise SafeError("An image file could not be opened.") from None
    parts = []
    frames = 0
    with img:
        for frame in ImageSequence.Iterator(img):
            frames += 1
            if frames > 50:
                break
            parts.append(ocr_array(_pil_to_rgb(frame)))
    ex = Extracted(text="\n\n".join(p for p in parts if p), kind="image", pages=frames, ocr_pages=frames, images_ocr=1)
    if not ex.text.strip():
        ex.notes.append("OCR found no text.")
    return ex


def _pdf(src: Path | bytes, depth: int = 0, budget: list[int] | None = None) -> Extracted:
    """``budget`` holds how many more attached files may be read, shared by every level."""
    import pymupdf

    budget = [MAX_ATTACHMENTS] if budget is None else budget
    try:
        doc = pymupdf.open(src) if isinstance(src, Path) else pymupdf.open(stream=src, filetype="pdf")
    except Exception:
        raise SafeError("A PDF could not be opened.") from None
    # Expand ligatures ("ﬃ" -> "ffi") so names typeset with them still match.
    flags = pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES
    with doc:
        if doc.needs_pass:
            raise SafeError("A PDF is password-protected.")
        ex = Extracted(text="", kind="pdf", pages=doc.page_count)
        parts = []
        for i, page in enumerate(doc, 1):
            text = page.get_text("text", flags=flags) or ""
            chunk = [f"--- page {i} ---"]
            if len(text.strip()) >= MIN_TEXT_CHARS:
                ex.text_pages += 1
                chunk.append(text.strip())
                page_area = abs(page.rect.width * page.rect.height) or 1.0
                for img in page.get_images(full=True):
                    try:
                        rects = page.get_image_rects(img[0])
                    except Exception:
                        continue
                    for rect in rects:
                        rect = rect & page.rect
                        if rect.is_empty or abs(rect.width * rect.height) / page_area < REGION_MIN_SHARE:
                            continue
                        found = ocr_array(_pixmap_to_rgb(page.get_pixmap(dpi=REGION_DPI, clip=rect, alpha=False)))
                        if found.strip():
                            ex.ocr_regions += 1
                            chunk.append("[text in embedded image]\n" + found)
            else:
                ex.ocr_pages += 1
                found = ocr_array(_pixmap_to_rgb(page.get_pixmap(dpi=PAGE_DPI, alpha=False)))
                chunk.append("[OCR]\n" + found if found.strip() else "[OCR found no text on this page]")
            parts.append("\n".join(chunk))
        try:
            attached = _attached_files(doc)
        except Exception:
            log.exception("could not list a PDF's attached files")
            attached = []
            ex.attachments_failed += 1
            ex.notes.append("The PDF's attached files could not be listed (details in run.log).")
        for n, (names, xref) in enumerate(attached, 1):
            parts.append(_pdf_attachment(doc, n, names, xref, ex, depth, budget))
        ex.text = "\n\n".join(parts)
        return ex


def _attached_files(doc) -> list[tuple[list[str], int]]:
    """Each file attached to a PDF, once: (the names it's filed under, the xref of its data).

    Covers the EmbeddedFiles name tree (a portfolio's documents) and paperclip
    annotations on pages. MuPDF's own tree loader is used because PyMuPDF's
    embfile_* calls miss a tree split into /Kids, and rescan the tree on every call.
    """
    from pymupdf import mupdf

    if not doc.is_pdf:  # MuPDF opens an image or HTML file named .pdf as what it really is
        return []
    pdf = doc.this if isinstance(doc.this, mupdf.PdfDocument) else mupdf.PdfDocument(doc.this)
    name = mupdf.pdf_new_name
    tree = mupdf.pdf_load_name_tree(pdf, name("EmbeddedFiles"))
    specs = []
    for i in range(mupdf.pdf_dict_len(tree)):
        key = mupdf.pdf_dict_get_key(tree, i)
        specs.append((mupdf.pdf_to_name(key) or mupdf.pdf_to_text_string(key), mupdf.pdf_dict_get_val(tree, i)))
    for p in range(mupdf.pdf_count_pages(pdf)):
        annots = mupdf.pdf_dict_get(mupdf.pdf_lookup_page_obj(pdf, p), name("Annots"))
        for j in range(mupdf.pdf_array_len(annots)):
            annot = mupdf.pdf_array_get(annots, j)
            if mupdf.pdf_name_eq(mupdf.pdf_dict_get(annot, name("Subtype")), name("FileAttachment")):
                specs.append(("", mupdf.pdf_dict_get(annot, name("FS"))))
    files, seen = [], set()
    for key, fs in specs:
        ef = mupdf.pdf_dict_get(fs, name("EF"))
        stream = mupdf.pdf_dict_get(ef, name("F"))
        if not mupdf.pdf_is_stream(stream):
            stream = mupdf.pdf_dict_get(ef, name("UF"))
        xref = mupdf.pdf_to_num(stream) if mupdf.pdf_is_stream(stream) else 0
        if xref in seen:  # one file listed twice (or a tree that repeats itself) is read once
            continue
        if xref:
            seen.add(xref)
        files.append(([mupdf.pdf_dict_get_text_string(fs, name(k)) for k in ("UF", "F")] + [key], xref))
    return files


_MAGIC = [(b"\x89PNG", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF8", ".gif"), (b"II*\x00", ".tif"), (b"MM\x00*", ".tif")]


def _sniff(data: bytes) -> str:
    """A supported extension for an attached file whose names don't give one, or ""."""
    if b"%PDF-" in data[:1024]:  # readers allow junk before the header
        return ".pdf"
    return next((ext for magic, ext in _MAGIC if data.startswith(magic)), "")


def _pdf_attachment(doc, n: int, names: list[str], xref: int, ex: Extracted, depth: int, budget: list[int]) -> str:
    """Read one file attached to a PDF and fold its counts into ``ex``.

    The attached file's names never reach the output: they may contain a name, and
    only an extension is needed. A file that can't be read is counted as failed
    rather than failing the whole PDF. Notes (private: review sheet only) name the
    file and say why.
    """
    head = f"--- attached file {n} ---"
    label = f"Attached file {n}" + next((f" ({nm})" for nm in names if nm), "")

    def skip(why: str, failed: bool = True) -> str:
        if failed:
            ex.attachments_failed += 1
        else:
            ex.attachments_skipped += 1
        ex.notes.append(f"{label} not processed: {why}")
        return head + "\n[not processed]"

    if not xref:
        return skip("it's a link to a file outside the PDF.", failed=False)
    if depth >= MAX_ATTACHMENT_DEPTH:
        return skip("nested too deep.")
    if budget[0] <= 0:
        return skip(f"more than {MAX_ATTACHMENTS} attached files in one document.")
    budget[0] -= 1
    try:
        ext = next((s for s in (Path(nm).suffix.lower() for nm in names if nm) if s in SUPPORTED), "")
        if not ext:
            kind, size = doc.xref_get_key(xref, "Params/Size")
            if kind == "int" and int(size) > SNIFF_MAX_BYTES:
                return skip("unsupported type.", failed=False)
        data = doc.xref_stream(xref) or b""
        ext = ext or _sniff(data)
        if not ext:
            return skip("unsupported type.", failed=False)
        sub = _extract(data, ext, depth + 1, budget)
    except SafeError as e:
        return skip(str(e))
    except Exception:  # details (which may quote the file) go to the private run.log only
        log.exception("attached file %d could not be read", n)
        return skip("it could not be read (details in run.log).")
    ex.attachments += 1 + sub.attachments
    ex.attachments_skipped += sub.attachments_skipped
    ex.attachments_failed += sub.attachments_failed
    ex.ocr_regions += sub.ocr_regions
    text = sub.text
    if sub.kind == "image":
        # OCR'd like an image on a page: it isn't an image file in the matter.
        ex.ocr_regions += 1
        text = "[OCR]\n" + text if text.strip() else "[OCR found no text]"
    elif sub.kind == "pdf":
        ex.pages += sub.pages
        ex.text_pages += sub.text_pages
        ex.ocr_pages += sub.ocr_pages
    ex.notes.extend(f"{label}: {note}" for note in sub.notes)
    return f"{head}\n{text}"


_BOMS = [
    (b"\xff\xfe\x00\x00", "utf-32-le"), (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"),
]


def _decode(data: bytes) -> str:
    """Decode a text file. Handles BOMs and BOM-less UTF-16 (PowerShell 5's ``>``)."""
    for bom, enc in _BOMS:
        if data.startswith(bom):
            return data.decode(enc, errors="replace")
    sample = data[:4096]
    if sample and sample.count(0) > len(sample) // 4:
        # Mostly-NUL text is UTF-16 without a BOM: NULs sit in the odd or even bytes.
        enc = "utf-16-le" if sample[1::2].count(0) > sample[0::2].count(0) else "utf-16-be"
        text = data.decode(enc, errors="replace")
        if "\x00" not in text:
            return text
        raise SafeError("A text file's encoding wasn't recognized.")
    for enc in ("utf-8", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


_BLOCK_TAGS = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table",
               "ul", "ol", "blockquote", "pre", "section", "article", "header", "footer", "hr"}


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.out.append("\n")
        elif tag in ("td", "th"):
            # Adjacent cells must not fuse ("Account" + "jexample"). Inline tags get
            # no space, as in a browser, so a name split across two <span>s stays whole.
            self.out.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _HTMLText()
    p.feed(html)
    lines = (re.sub(r"[ \t\xa0]+", " ", line).strip() for line in "".join(p.out).split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# The line _eml writes for each attachment. The pipeline analyzes the name in it the
# way it analyzes a file name, so "RobertFakename2024.docx" doesn't pass as one token.
ATTACHMENT_NAME_LINE = re.compile(r"^\[attachment not processed: (.+)\]$", re.M)


def _eml(src: Path | bytes) -> Extracted:
    msg = email.message_from_bytes(_read(src), policy=policy.default)
    lines = []
    for h in ("From", "To", "Cc", "Bcc", "Reply-To", "Date", "Subject"):
        if msg[h]:
            lines.append(f"{h}: {msg[h]}")
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        try:
            content = body.get_content()
        except Exception:
            content = _decode(body.get_payload(decode=True) or b"")
        text = html_to_text(content) if body.get_content_type() == "text/html" else content
    ex = Extracted(text="", kind="email")
    for part in msg.iter_attachments():
        ex.attachments_skipped += 1
        # Underscores become spaces, as in file names, and the name stays on one line.
        lines_name = " ".join(re.sub(r"_+", " ", part.get_filename() or "").split()) or "(unnamed)"
        text += f"\n[attachment not processed: {lines_name}]"
    ex.text = "\n".join(lines) + "\n\n" + text.strip()
    if ex.attachments_skipped:
        ex.notes.append(f"{ex.attachments_skipped} attachment(s) not processed. Save them into the matter folder to include them.")
    return ex


def _docx(src: Path | bytes) -> Extracted:
    try:
        import docx
    except ImportError:
        raise SafeError("python-docx is not installed; DOCX files are skipped.") from None
    try:
        d = docx.Document(io.BytesIO(_read(src)))
    except Exception:
        raise SafeError("A DOCX file could not be opened.") from None
    parts = []
    for section in d.sections:
        for para in section.header.paragraphs:
            if para.text.strip():
                parts.append(para.text)
    parts.extend(p.text for p in d.paragraphs)
    for table in d.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    for section in d.sections:
        for para in section.footer.paragraphs:
            if para.text.strip():
                parts.append(para.text)
    ex = Extracted(text="\n".join(parts), kind="docx")
    ex.notes.append("DOCX: footnotes, comments and text boxes are not extracted.")
    return ex


def _extract(src: Path | bytes, ext: str, depth: int = 0, budget: list[int] | None = None) -> Extracted:
    """``src`` is a file on disk or the bytes of a file attached to a PDF; ``ext`` picks the reader."""
    if ext == ".pdf":
        return _pdf(src, depth, budget)
    if ext in IMAGE_EXT:
        return _image(src)
    if ext in TEXT_EXT:
        return Extracted(text=_decode(_read(src)), kind="text")
    if ext == ".eml":
        return _eml(src)
    if ext == ".docx":
        return _docx(src)
    raise SafeError("Unsupported file type.")


def extract(path: Path) -> Extracted:
    ex = _extract(path, path.suffix.lower())
    ex.text = normalize_text(ex.text)
    return ex
