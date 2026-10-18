"""Turn a source file into plain text, on this machine.

- PDF: PyMuPDF text layer. A page with (almost) no text is rendered and OCR'd.
  Large images on a text page are OCR'd too, so text inside an embedded
  screenshot isn't lost.
- PNG/JPG/WEBP/BMP/TIFF/GIF: RapidOCR (ONNX models bundled in the package).
- TXT/MD: read as text. EML: headers and body; attachments are named, not opened.
- DOCX: paragraphs, tables, headers and footers (python-docx, if installed).

Only text comes out. Images are never copied anywhere.
"""

from __future__ import annotations

from redact_local import netguard

netguard.install()

import email  # noqa: E402
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


@dataclass
class Extracted:
    text: str
    kind: str
    pages: int = 0
    text_pages: int = 0
    ocr_pages: int = 0
    ocr_regions: int = 0
    images_ocr: int = 0
    attachments_skipped: int = 0
    notes: list[str] = field(default_factory=list)  # private: shown in review.html only


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

def _image(path: Path) -> Extracted:
    from PIL import Image, ImageSequence, UnidentifiedImageError

    try:
        img = Image.open(path)
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


def _pdf(path: Path) -> Extracted:
    import pymupdf

    try:
        doc = pymupdf.open(path)
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
        ex.text = "\n\n".join(parts)
        return ex


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


def _eml(path: Path) -> Extracted:
    msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
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
        lines_name = part.get_filename() or "(unnamed)"
        text += f"\n[attachment not processed: {lines_name}]"
    ex.text = "\n".join(lines) + "\n\n" + text.strip()
    if ex.attachments_skipped:
        ex.notes.append(f"{ex.attachments_skipped} attachment(s) not processed. Save them into the matter folder to include them.")
    return ex


def _docx(path: Path) -> Extracted:
    try:
        import docx
    except ImportError:
        raise SafeError("python-docx is not installed; DOCX files are skipped.") from None
    try:
        d = docx.Document(str(path))
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


def _extract(path: Path) -> Extracted:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _pdf(path)
    if ext in IMAGE_EXT:
        return _image(path)
    if ext in TEXT_EXT:
        return Extracted(text=_decode(path.read_bytes()), kind="text")
    if ext == ".eml":
        return _eml(path)
    if ext == ".docx":
        return _docx(path)
    raise SafeError("Unsupported file type.")


def extract(path: Path) -> Extracted:
    ex = _extract(path)
    ex.text = normalize_text(ex.text)
    return ex
