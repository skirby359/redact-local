"""exhibit: a court copy of an original PDF or image with only GR 31(e) identifiers removed.
Synthetic documents only."""

from __future__ import annotations

from conftest import CARD, CLIENT, SSN, assert_no_secrets, cli, render_png

from redact_local import exhibit

FAKE_DL = "WDLFAKE0X12Q"


def _text_pdf(path, lines):
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    for i, line in enumerate(lines):
        page.insert_text((72, 72 + 20 * i), line, fontsize=11)
    doc.save(str(path))
    doc.close()
    return path


def _scan_pdf(path, tmp_path, lines):
    import pymupdf
    from PIL import Image

    png = render_png(tmp_path / "scan.png", lines, size=48)
    with Image.open(png) as img:
        w, h = img.size
    doc = pymupdf.open()
    page = doc.new_page(width=w * 72 / 300, height=h * 72 / 300)
    page.insert_image(page.rect, filename=str(png))
    doc.save(str(path))
    doc.close()
    return path


def _page_text(path, ocr=False):
    import pymupdf

    from redact_local import extract

    with pymupdf.open(path) as d:
        page = d[0]
        if not ocr:
            return page.get_text("text")
        return extract.ocr_array(extract._pixmap_to_rgb(page.get_pixmap(dpi=300, alpha=False)))


def test_text_layer_pdf_keeps_names_and_last_four(matter):
    src = _text_pdf(matter / "intake.pdf", [f"Client: {CLIENT}", f"SSN {SSN}, license {FAKE_DL}.",
                                             f"Card {CARD} on file."])
    res = exhibit.make_exhibit(src)
    assert res.output.parent == matter / "_redact" / "court" and res.output.name == "intake - court copy.pdf"
    assert (res.masked, res.withheld, res.pages, res.by_text, res.still_readable) == (2, 1, [1], 3, 0)
    text = _page_text(res.output)
    assert CLIENT in text                                   # names stay: this is the filed original
    assert "6012" in text and "219-44" not in text and "21944" not in text.replace("-", "")
    assert FAKE_DL not in text and "1111" in text and "4111 1111 1111" not in text
    assert src.read_bytes() != res.output.read_bytes()      # the original is untouched
    assert "219-44-6012" in _page_text(src)


def test_scanned_pdf_is_redacted_in_the_pixels(matter, tmp_path):
    src = _scan_pdf(matter / "scan.pdf", tmp_path, ["Benefits form", f"SSN {SSN}", f"Signed {CLIENT}"])
    res = exhibit.make_exhibit(src)
    assert res.ocr_pages == 1 and res.by_ocr == 1 and res.masked == 1 and res.still_readable == 0
    text = _page_text(res.output, ocr=True)
    assert "fakerton" in text.lower() and "219" not in text and "44-" not in text


def test_image_exhibit(matter):
    png = render_png(matter / "photo.png", [f"License {FAKE_DL}", f"Owner {CLIENT}"], size=48)
    res = exhibit.make_exhibit(png)
    assert res.output.name == "photo - court copy.png" and res.withheld == 1 and res.still_readable == 0
    from PIL import Image

    from redact_local import extract

    with Image.open(res.output) as img:
        text = extract.ocr_array(extract._pil_to_rgb(img))
    assert "fakerton" in text.lower() and "WDL" not in text.upper()


def test_values_the_key_already_holds_are_found(matter):
    key = matter / "_redact" / "key.csv"
    key.write_text("placeholder,category,value,ident,primary\nDRIVER-LICENSE-1,DRIVER-LICENSE,QZ-FAKE-7781,"
                   "DRIVER-LICENSE|qz-fake-7781,yes\n", encoding="utf-8")
    src = _text_pdf(matter / "ro.pdf", ["Repair order", "Lic QZ-FAKE-7781 Total 12345"])
    res = exhibit.make_exhibit(src)
    assert res.withheld == 1 and "QZ-FAKE" not in _page_text(res.output) and "12345" in _page_text(res.output)


def test_console_shows_counts_only(matter, tmp_path):
    src = _text_pdf(matter / "Quentin Fakerton intake.pdf", [f"SSN {SSN}", f"Client {CLIENT}"])
    proc = cli("exhibit", str(src), cwd=tmp_path)
    console = proc.stdout + proc.stderr
    assert proc.returncode == 0, console
    assert_no_secrets(console)
    assert "1 cut to the last four characters, 0 withheld, on page(s) 1" in proc.stdout
    assert (matter / "_redact" / "court" / "Quentin Fakerton intake - court copy.pdf").is_file()
