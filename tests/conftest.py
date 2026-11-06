"""Shared fixtures. Everything here is synthetic, with obviously fake names.

The network guard is installed for the whole test session, so every in-process
pipeline run in these tests runs with the guard on.
"""

from __future__ import annotations

from redact_local import netguard

netguard.install()

import atexit  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

# A copy of config/ without anyone's personal *.local.txt additions, so local
# lists can't change test results.
_CONFIG_TMP = Path(tempfile.mkdtemp(prefix="redact-local-config-"))
CONFIG = _CONFIG_TMP / "config"
shutil.copytree(REPO / "config", CONFIG, ignore=shutil.ignore_patterns("*.local.txt"))
atexit.register(shutil.rmtree, _CONFIG_TMP, True)

CLIENT = "Quentin Fakerton"
CLIENT_HANDLE = "qfakerton"
CLIENT_EMAIL = "quentin.fakerton@example.com"
STAFF = "Marisol Testwell"
STAFF_EMAIL = "mtestwell@example.org"
OTHER = "Robert Fakename"
PHONE = "(206) 555-0142"
SSN = "219-44-6012"
CARD = "4111 1111 1111 1111"
ADDRESS = "1234 Fakewood Ave NE, Apt 5"
DOB = "03/14/1988"
TICKET = "4471029"

# Every fake value that must never reach the console.
SECRETS = [
    "Quentin", "Fakerton", "qfakerton", "Marisol", "Testwell", "mtestwell", "Robert Fakename",
    "Fakename", "555-0142", "219-44-6012", "4111", "Fakewood", "03/14/1988", "4471029", "example.com",
    "example.org", "Zorbleflax",
]

NAMES_TXT = f"""# synthetic names.txt
{CLIENT} | @{CLIENT_HANDLE} | {CLIENT_EMAIL} => CLIENT
{STAFF} => MANAGER
"""

LETTER = f"""From: {STAFF} <{STAFF_EMAIL}>
To: {CLIENT_EMAIL}
Subject: Notice about your tenant portal account and x.com/{CLIENT_HANDLE}

Hi Quentin,

Your tenant portal account @{CLIENT_HANDLE} was reviewed by our Trust & Safety team. Support ticket #{TICKET}.
Call us at {PHONE}. Fakerton, Quentin is listed at {ADDRESS}, Seattle, WA 98105.
DOB: {DOB}. SSN {SSN}. Card {CARD}.
I also spoke with {OTHER} about it. Robert said the Northwest Justice Project might help.
See State v. Fakerton, 123 Wn.2d 456, 460 (1994) and RCW 9A.04.110.
"""


@pytest.fixture
def matter(tmp_path: Path) -> Path:
    """An empty matter folder with a names.txt. The name has no fake person in it,
    so a path printed by the CLI can't trip the console-leak checks."""
    m = tmp_path / "Matter 001"
    (m / "_redact").mkdir(parents=True)
    (m / "_redact" / "names.txt").write_text(NAMES_TXT, encoding="utf-8")
    return m


def run(m: Path, profile: str = "client", target: Path | None = None, **kw):
    from redact_local import pipeline

    return pipeline.run(target or m, profile, CONFIG, **kw)


def out_dir(m: Path) -> Path:
    return m.parent / f"{m.name} - redacted"


def outputs(m: Path) -> dict[str, str]:
    d = out_dir(m)
    return {str(p.relative_to(d)): p.read_text(encoding="utf-8") for p in sorted(d.rglob("*.txt"))}


def font(size: int = 40):
    from PIL import ImageFont

    for name in ("arial.ttf", "segoeui.ttf", "calibri.ttf"):
        path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def render_png(path: Path, lines: list[str], size: int = 40) -> Path:
    """Draw black text on white, like a screenshot."""
    from PIL import Image, ImageDraw

    f = font(size)
    width = max(int(f.getlength(line)) for line in lines) + 80
    height = len(lines) * int(size * 1.8) + 80
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((40, 40 + i * int(size * 1.8)), line, fill="black", font=f)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def make_pdf(path: Path, tmp_path: Path) -> Path:
    """Two pages: a text layer, then a scanned page with no text layer."""
    import pymupdf
    from PIL import Image

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), f"Letter to {CLIENT} at {CLIENT_EMAIL} about the lease.", fontsize=11)
    scan = render_png(tmp_path / "scan.png", ["Scanned page", f"Signed by {CLIENT}"], size=48)
    with Image.open(scan) as img:
        w, h = img.size
    # Sized so the 300 DPI render is the scan's own pixels: OCR on a full A4 page is ~5x slower.
    page2 = doc.new_page(width=w * 72 / 300, height=h * 72 / 300)
    page2.insert_image(page2.rect, filename=str(scan))
    doc.save(str(path))
    doc.close()
    return path


def make_portfolio(path: Path, tmp_path: Path) -> Path:
    """A PDF Portfolio: a cover page telling you to open it in Acrobat, with the real
    documents embedded. The embedded file names carry the fake client's name."""
    import pymupdf

    inner = make_pdf(tmp_path / "inner.pdf", tmp_path)
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "This portfolio is best viewed in a viewer that supports portfolios.", fontsize=11)
    doc.embfile_add("letter", inner.read_bytes(), filename=f"{CLIENT} letter.pdf")
    doc.embfile_add("note", f"Call {CLIENT} at {CLIENT_EMAIL}.".encode(), filename=f"{CLIENT} note.txt")
    doc.embfile_add("sheet", b"PK\x03\x04 not really a spreadsheet", filename=f"{CLIENT} sheet.xlsx")
    doc.xref_set_key(doc.pdf_catalog(), "Collection", "<</Type/Collection/View/D>>")
    doc.save(str(path))
    doc.close()
    return path


def cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run the CLI in a fresh process exactly as the skill does (python -I -m redact_local)."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, "-I", "-m", "redact_local", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd, env=env, timeout=600,
    )


def assert_no_secrets(text: str) -> None:
    low = text.lower()
    leaked = [s for s in SECRETS if s.lower() in low]
    assert not leaked, f"console output contained {len(leaked)} fake value(s)"
