"""Court copies of original documents: GR 31(e) identifiers blacked out, nothing else.

    redact-local exhibit <original.pdf|.jpg|.png|.tif> [--matter <folder>]

An exhibit is filed as the original document, not as redact-local's extracted text. This
makes a copy of the original with only the identifiers Washington GR 31(e)(1) keeps out of
court files removed: SSNs and financial account numbers cut to their last four characters,
driver's licence numbers withheld (the [court_filing] table in profiles.toml decides).
Names and everything else stay, as they would in the filed original.

What is looked for: the values in those categories the matter's key already holds, plus
SSN, card, IBAN and Washington driver's licence patterns. Where:
  - a PDF page with a text layer: in the text itself, so each box is exact;
  - a scanned page or an image: in OCR lines, boxed by the value's share of the line, padded.
Redaction is real: text under a box is removed and image pixels are blacked out. The copy
is then read again (text layer and OCR) and anything still legible is counted.

The copy goes to <matter>\\_redact\\court\\ (private: it still holds names). The console shows
counts and page numbers only. OCR can miss a value it misreads, so look at every page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from redact_local.config import config_dir, load_court_filing
from redact_local.errors import SafeError
from redact_local.pipeline import PRIVATE, matter_paths
from redact_local.placeholders import Registry

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
COURT_DIR = "court"
_SSN = re.compile(r"(?<![\d-])\d{3}[- ]\d{2}[- ]\d{4}(?![\d-])")
_CARD = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")
_IBAN = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?(?![A-Z0-9])")
_WA_DL = re.compile(r"(?<![A-Z0-9])WDL[A-Z0-9]{9}(?![A-Z0-9])")
_DIGITS = {"SSN", "CARD", "PHONE"}


@dataclass
class ExhibitResult:
    output: Path
    masked: int = 0          # values cut to the last four
    withheld: int = 0        # values blacked out entirely
    pages: list[int] = field(default_factory=list)   # 1-based pages (or frames) with a redaction
    by_text: int = 0
    by_ocr: int = 0
    ocr_pages: int = 0
    still_readable: int = 0  # hidden parts the read-back could still find


def _luhn(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return len(digits) >= 13 and total % 10 == 0


def _iban_ok(value: str) -> bool:
    s = value.replace(" ", "")
    s = s[4:] + s[:4]
    try:
        return int("".join(str(int(c, 36)) for c in s)) % 97 == 1
    except ValueError:
        return False


class _Finder:
    """Find the values to remove in one line of text: known values from the key, and patterns."""

    def __init__(self, known: dict[str, list[str]], last4: frozenset[str], withhold: frozenset[str]):
        self.last4, self.withhold = last4, withhold
        self.known: list[tuple[re.Pattern, str]] = []
        for cat, values in known.items():
            for v in values:
                rx = self._value_regex(v, cat)
                if rx is not None:
                    self.known.append((rx, cat))

    @staticmethod
    def _value_regex(value: str, category: str) -> re.Pattern | None:
        if category in _DIGITS:
            digits = re.sub(r"\D", "", value)
            if len(digits) < 6:
                return None
            return re.compile(r"(?<!\d)" + r"[\s.-]{0,2}".join(digits) + r"(?!\d)")
        chars = [c for c in value if c.isalnum()]
        if len(chars) < 5:
            return None
        return re.compile(r"(?<![A-Za-z0-9])" + r"[\s.-]{0,2}".join(re.escape(c) for c in chars) + r"(?![A-Za-z0-9])", re.I)

    def find(self, s: str) -> list[tuple[int, int, str]]:
        cats = self.last4 | self.withhold
        found = [(m.start(), m.end(), cat) for rx, cat in self.known if cat in cats for m in rx.finditer(s)]
        if "SSN" in cats:
            found += [(m.start(), m.end(), "SSN") for m in _SSN.finditer(s)]
        if "CARD" in cats:
            found += [(m.start(), m.end(), "CARD") for m in _CARD.finditer(s) if _luhn(m.group(0))]
        if "IBAN" in cats:
            found += [(m.start(), m.end(), "IBAN") for m in _IBAN.finditer(s) if _iban_ok(m.group(0))]
        if "DRIVER-LICENSE" in cats:
            found += [(m.start(), m.end(), "DRIVER-LICENSE") for m in _WA_DL.finditer(s)]
        found.sort(key=lambda t: (t[0], -(t[1] - t[0])))
        out = []
        for a, b, cat in found:
            if out and a < out[-1][1]:
                continue        # overlaps a longer or earlier one
            out.append((a, b, cat))
        return out

    def hidden(self, s: str, a: int, b: int, cat: str) -> tuple[int, int, str]:
        """(start, end, mode) of the part to black out: all of it, or all but the last four."""
        if cat in self.withhold:
            return a, b, "withhold"
        pos = [k for k in range(a, b) if s[k].isalnum()]
        if len(pos) <= 4:
            return a, b, "last4"
        return a, pos[-4], "last4"


def _hidden_regex(chars: str) -> re.Pattern:
    # Left boundary only: the visible last four can follow the hidden part directly.
    return re.compile(r"(?<![A-Za-z0-9])" + r"[\W_]{0,3}".join(re.escape(c) for c in chars), re.I)


def _known_values(matter: Path, cats: frozenset[str]) -> dict[str, list[str]]:
    reg = Registry(matter / PRIVATE / "key.csv")
    out: dict[str, list[str]] = {}
    for ph, rows in reg.rows.items():
        cat = reg.category.get(ph, "")
        if cat in cats:
            out.setdefault(cat, []).extend(rows)
    return out


def make_exhibit(original, matter=None, config=None) -> ExhibitResult:
    src = Path(original).expanduser()
    if not src.is_file():
        raise SafeError("File not found.")
    src = src.resolve()
    ext = src.suffix.lower()
    if ext != ".pdf" and ext not in IMAGE_EXT:
        raise SafeError("exhibit supports PDF and image files (.jpg, .png, .tif, .bmp).")
    if src.stem.endswith(" - court copy"):
        raise SafeError("That is already a court copy.")
    if matter is None:
        matter, _scope = matter_paths(src)
    else:
        matter, _scope = matter_paths(matter)
    if not (matter / PRIVATE).is_dir():
        raise SafeError("No matter folder found for that file. Pass --matter <folder>.")
    rules = load_court_filing(config_dir(config))
    finder = _Finder(_known_values(matter, rules.last4 | rules.withhold), rules.last4, rules.withhold)
    out_dir = matter / PRIVATE / COURT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{src.stem} - court copy{ext}"
    result = ExhibitResult(out)
    if ext == ".pdf":
        _pdf(src, out, finder, result)
    else:
        _image(src, out, finder, result)
    return result


# ── OCR helpers ────────────────────────────────────────────────────────────

def _ocr_lines(rgb) -> list[tuple[float, float, float, float, str]]:
    """(x0, y0, x1, y1, text) for every OCR line, in the array's pixels."""
    import numpy as np

    from redact_local.extract import _ocr_engine

    result, _ = _ocr_engine()(np.ascontiguousarray(rgb[:, :, ::-1]))
    out = []
    for box, text, _score in result or []:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append((min(xs), min(ys), max(xs), max(ys), text))
    return out


def _ocr_boxes(lines, finder: _Finder, result: ExhibitResult) -> list[tuple[float, float, float, float]]:
    """Pixel boxes to black out, from OCR lines: the value's share of the line width, padded."""
    boxes = []
    for x0, y0, x1, y1, text in lines:
        n = max(len(text), 1)
        cw, h = (x1 - x0) / n, y1 - y0
        for a, b, cat in finder.find(text):
            ha, hb, mode = finder.hidden(text, a, b, cat)
            boxes.append((x0 + cw * ha - 0.5 * cw, y0 - 0.15 * h, x0 + cw * hb + 0.5 * cw, y1 + 0.15 * h))
            _count(result, mode)
            result.by_ocr += 1
    return boxes


def _count(result: ExhibitResult, mode: str) -> None:
    if mode == "withhold":
        result.withheld += 1
    else:
        result.masked += 1


def _residue(text: str, finder: _Finder, hidden: list[str]) -> int:
    """How many hidden parts can still be read in ``text``."""
    return sum(1 for chars in hidden if chars and _hidden_regex(chars).search(text))


# ── PDF ────────────────────────────────────────────────────────────────────

def _pdf(src: Path, out: Path, finder: _Finder, result: ExhibitResult) -> None:
    import pymupdf

    from redact_local.extract import MIN_TEXT_CHARS, PAGE_DPI, _ocr_dpi, _pixmap_to_rgb

    try:
        doc = pymupdf.open(src)
    except Exception:
        raise SafeError("The PDF could not be opened.") from None
    with doc:
        if doc.needs_pass:
            raise SafeError("The PDF is password-protected.")
        hidden_by_page: dict[int, tuple[list[str], bool]] = {}
        for pno, page in enumerate(doc):
            rotation = page.rotation
            if rotation:
                page.set_rotation(0)      # work in plain page coordinates; put it back after
            hidden: list[str] = []
            n_before = result.masked + result.withheld
            text = page.get_text("text") or ""
            used_ocr = len(text.strip()) < MIN_TEXT_CHARS
            # Text layer: exact character boxes.
            for line_chars in _text_lines(page):
                s = "".join(c for c, _r in line_chars)
                for a, b, cat in finder.find(s):
                    ha, hb, mode = finder.hidden(s, a, b, cat)
                    rects = [r for _c, r in line_chars[ha:hb]]
                    if rects:
                        box = pymupdf.Rect(rects[0])
                        for r in rects[1:]:
                            box |= r
                        page.add_redact_annot(box, fill=(0, 0, 0))
                        hidden.append("".join(ch for ch in s[ha:hb] if ch.isalnum()))
                        _count(result, mode)
                        result.by_text += 1
            # Scanned page: OCR boxes.
            if used_ocr:
                result.ocr_pages += 1
                dpi = _ocr_dpi(page.rect, PAGE_DPI)
                pix = page.get_pixmap(dpi=dpi, alpha=False)
                sx, sy = page.rect.width / pix.width, page.rect.height / pix.height
                lines = _ocr_lines(_pixmap_to_rgb(pix))
                for x0, y0, x1, y1 in _ocr_boxes(lines, finder, result):
                    page.add_redact_annot(pymupdf.Rect(page.rect.x0 + x0 * sx, page.rect.y0 + y0 * sy,
                                                       page.rect.x0 + x1 * sx, page.rect.y0 + y1 * sy), fill=(0, 0, 0))
                for x0, y0, x1, y1, line in lines:
                    for a, b, cat in finder.find(line):
                        ha, hb, _m = finder.hidden(line, a, b, cat)
                        hidden.append("".join(ch for ch in line[ha:hb] if ch.isalnum()))
            if result.masked + result.withheld > n_before:
                page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                                      graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                      text=pymupdf.PDF_REDACT_TEXT_REMOVE)
                result.pages.append(pno + 1)
                hidden_by_page[pno] = (hidden, used_ocr)
            if rotation:
                page.set_rotation(rotation)
        doc.save(str(out), garbage=3, deflate=True)
    # Read the copy back: text layer, and OCR where the page was scanned.
    with pymupdf.open(out) as copy:
        for pno, (hidden, used_ocr) in hidden_by_page.items():
            page = copy[pno]
            text = page.get_text("text") or ""
            if used_ocr:
                dpi = _ocr_dpi(page.rect, PAGE_DPI)
                text += "\n" + "\n".join(t for *_b, t in _ocr_lines(_pixmap_to_rgb(page.get_pixmap(dpi=dpi, alpha=False))))
            result.still_readable += _residue(text, finder, hidden)


def _text_lines(page) -> list[list[tuple[str, tuple]]]:
    """Each text line as [(char, bbox)], in reading order."""
    out = []
    raw = page.get_text("rawdict", flags=0)
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            chars = []
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    chars.append((ch["c"], tuple(ch["bbox"])))
            if chars:
                out.append(chars)
    return out


# ── Images ─────────────────────────────────────────────────────────────────

def _image(src: Path, out: Path, finder: _Finder, result: ExhibitResult) -> None:
    import numpy as np
    from PIL import Image, ImageDraw, ImageSequence, UnidentifiedImageError

    from redact_local.extract import MAX_OCR_SIDE

    try:
        img = Image.open(src)
    except (UnidentifiedImageError, OSError):
        raise SafeError("The image could not be opened.") from None
    frames, hidden_by_frame = [], {}
    with img:
        fmt = img.format
        for fno, frame in enumerate(ImageSequence.Iterator(img)):
            rgb = frame.convert("RGB")
            scale = 1.0
            work = rgb
            if max(rgb.size) > MAX_OCR_SIDE:
                scale = max(rgb.size) / MAX_OCR_SIDE
                work = rgb.resize((round(rgb.width / scale), round(rgb.height / scale)), Image.Resampling.LANCZOS)
            lines = _ocr_lines(np.array(work))
            result.ocr_pages += 1
            n_before = result.masked + result.withheld
            boxes = _ocr_boxes(lines, finder, result)
            if boxes:
                draw = ImageDraw.Draw(rgb)
                for x0, y0, x1, y1 in boxes:
                    draw.rectangle([x0 * scale, y0 * scale, x1 * scale, y1 * scale], fill=(0, 0, 0))
            if result.masked + result.withheld > n_before:
                result.pages.append(fno + 1)
                hidden_by_frame[fno] = ["".join(ch for ch in line[ha:hb] if ch.isalnum())
                                        for *_b, line in lines for a, b, cat in finder.find(line)
                                        for ha, hb, _m in [finder.hidden(line, a, b, cat)]]
            frames.append(rgb)
    # Saved without the original's metadata (EXIF can hold a location and a device owner).
    save = {"quality": 95} if fmt == "JPEG" else {}
    if len(frames) > 1:
        frames[0].save(out, format=fmt, save_all=True, append_images=frames[1:], **save)
    else:
        frames[0].save(out, format=fmt, **save)
    with Image.open(out) as back:
        for fno, back_frame in enumerate(ImageSequence.Iterator(back)):
            if fno in hidden_by_frame:
                text = "\n".join(t for *_b, t in _ocr_lines(np.array(_fit(back_frame.convert("RGB"), MAX_OCR_SIDE))))
                result.still_readable += _residue(text, finder, hidden_by_frame[fno])


def _fit(img, side: int):
    if max(img.size) <= side:
        return img
    img = img.copy()
    img.thumbnail((side, side))
    return img
