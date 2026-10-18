"""Extraction: OCR on a rendered PNG, PDF text layer, scanned-page fallback, email, docx."""

from __future__ import annotations

from conftest import CLIENT, CLIENT_EMAIL, outputs, render_png, run

from redact_local import extract


def test_ocr_reads_a_rendered_png(tmp_path):
    png = render_png(tmp_path / "shot.png", ["Payment overdue", f"Contact {CLIENT}", "Ticket 4471029"])
    ex = extract.extract(png)
    assert ex.kind == "image"
    text = ex.text.lower()
    assert "overdue" in text and "fakerton" in text and "4471029" in text


def test_ocr_output_is_redacted(matter):
    render_png(matter / "Screenshot 1.png", [f"Hi {CLIENT}", "your rent for @qfakerton is overdue"])
    run(matter)
    out = outputs(matter)["Screenshot 1.png.txt"]
    assert "[CLIENT]" in out and "overdue" in out.lower()
    assert "fakerton" not in out.lower()


def _make_pdf(path, tmp_path):
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), f"Letter to {CLIENT} at {CLIENT_EMAIL} about the lease.", fontsize=11)
    scan = render_png(tmp_path / "scan.png", ["Scanned page", f"Signed by {CLIENT}"], size=48)
    page2 = doc.new_page()
    page2.insert_image(page2.rect, filename=str(scan))
    doc.save(str(path))
    doc.close()


def test_pdf_text_layer_and_scanned_page_fallback(tmp_path):
    pdf = tmp_path / "doc.pdf"
    _make_pdf(pdf, tmp_path)
    ex = extract.extract(pdf)
    assert ex.pages == 2 and ex.text_pages == 1 and ex.ocr_pages == 1
    assert CLIENT_EMAIL in ex.text
    assert "signed by" in ex.text.lower()


def test_pdf_output_is_redacted(matter, tmp_path):
    _make_pdf(matter / "doc.pdf", tmp_path)
    result = run(matter)
    out = outputs(matter)["doc.pdf.txt"]
    assert "fakerton" not in out.lower()
    assert out.count("[CLIENT") >= 2 and "[OCR]" in out
    assert result.summary["pages"]["pdf_ocr"] == 1


def test_eml_and_docx(matter):
    import docx

    (matter / "mail.eml").write_text(
        f"From: {CLIENT} <{CLIENT_EMAIL}>\nTo: support@example.org\nSubject: Appeal\n"
        "Content-Type: text/plain\n\nPlease restore my account.\n", encoding="utf-8")
    d = docx.Document()
    d.add_paragraph(f"Declaration of {CLIENT}")
    d.save(str(matter / "decl.docx"))
    run(matter)
    outs = outputs(matter)
    assert "[CLIENT]" in outs["mail.eml.txt"] and "[CLIENT-EMAIL]" in outs["mail.eml.txt"]
    assert "Declaration of [CLIENT]" in outs["decl.docx.txt"]
