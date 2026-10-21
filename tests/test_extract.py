"""Extraction: OCR on a rendered PNG, PDF text layer, scanned-page fallback, PDF attachments, email, docx."""

from __future__ import annotations

from conftest import CLIENT, CLIENT_EMAIL, make_pdf, make_portfolio, outputs, render_png, run

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


def test_pdf_text_layer_and_scanned_page_fallback(tmp_path):
    pdf = make_pdf(tmp_path / "doc.pdf", tmp_path)
    ex = extract.extract(pdf)
    assert ex.pages == 2 and ex.text_pages == 1 and ex.ocr_pages == 1
    assert CLIENT_EMAIL in ex.text
    assert "signed by" in ex.text.lower()


def test_pdf_output_is_redacted(matter, tmp_path):
    make_pdf(matter / "doc.pdf", tmp_path)
    result = run(matter)
    out = outputs(matter)["doc.pdf.txt"]
    assert "fakerton" not in out.lower()
    assert out.count("[CLIENT") >= 2 and "[OCR]" in out
    assert result.summary["pages"]["pdf_ocr"] == 1


def test_pdf_portfolio_reads_embedded_files(tmp_path):
    # Regression: a portfolio's only page is a cover sheet, so its documents were lost.
    ex = extract.extract(make_portfolio(tmp_path / "portfolio.pdf", tmp_path))
    assert ex.attachments == 2 and ex.attachments_skipped == 1     # the .xlsx is skipped
    assert ex.attachments_failed == 0 and any("sheet.xlsx" in n for n in ex.notes)  # notes are private
    assert ex.pages == 3 and ex.text_pages == 2 and ex.ocr_pages == 1
    assert CLIENT_EMAIL in ex.text and "signed by" in ex.text.lower() and "call" in ex.text.lower()
    # Embedded file names may hold names: only their contents reach the text.
    assert "letter.pdf" not in ex.text and "note.txt" not in ex.text and "sheet.xlsx" not in ex.text


def test_pdf_portfolio_output_is_redacted(matter, tmp_path):
    make_portfolio(matter / "portfolio.pdf", tmp_path)
    result = run(matter)
    out = outputs(matter)["portfolio.pdf.txt"]
    assert "fakerton" not in out.lower()
    assert "[CLIENT]" in out and "[CLIENT-EMAIL]" in out and "signed by" in out.lower()
    files = result.summary["files"]
    assert files["attachments_read"] == 2 and files["attachments_not_processed"] == 1 and files["attachments_failed"] == 0


def _pdf_bytes(text: str, files=(), **save) -> bytes:
    """One page saying ``text``, with ``files`` ((name, bytes) pairs) in its EmbeddedFiles list."""
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text, fontsize=11)
    for i, (name, data) in enumerate(files):
        doc.embfile_add(f"file{i}", data, filename=name)
    out = doc.tobytes(**save)
    doc.close()
    return out


def test_pdf_named_file_that_is_really_an_image_is_still_read(tmp_path):
    # Regression: MuPDF opens it as an image document, and the attachment lookup crashed on that.
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(render_png(tmp_path / "shot.png", [f"Hi {CLIENT}"]).read_bytes())
    ex = extract.extract(pdf)
    assert ex.kind == "pdf" and ex.ocr_pages == 1 and "fakerton" in ex.text.lower()


def test_unreadable_attachments_are_counted_not_fatal(tmp_path):
    # Regression: one attachment that raised anything but a SafeError failed the whole PDF.
    import pymupdf

    png = render_png(tmp_path / "shot.png", ["Payment overdue"]).read_bytes()
    locked = _pdf_bytes("A locked letter to the tenant about the lease.",
                        encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    pdf = tmp_path / "bundle.pdf"
    pdf.write_bytes(_pdf_bytes("Cover sheet for the tenant's bundle of letters.", [
        ("photo.png", png[: len(png) // 2]),     # truncated: Pillow fails while decoding it
        ("locked.pdf", locked),                   # password-protected
        ("note.txt", f"Call {CLIENT}.".encode()),
    ]))
    ex = extract.extract(pdf)
    assert ex.attachments == 1 and ex.attachments_failed == 2 and ex.attachments_skipped == 0
    assert "cover sheet" in ex.text.lower() and CLIENT in ex.text


def test_attachments_in_a_kids_tree_or_on_a_page_are_read_once_each(tmp_path):
    # PyMuPDF's embfile_* calls see neither a name tree split into /Kids nor paperclip
    # attachments, so both used to be dropped without being counted.
    import pymupdf

    doc = pymupdf.open(stream=_pdf_bytes("Cover sheet for the tenant's bundle of letters.", [
        ("note.txt", f"Call {CLIENT}.".encode()), ("email.txt", f"Write to {CLIENT_EMAIL}.".encode())]))
    cat = doc.pdf_catalog()
    _, names = doc.xref_get_key(cat, "Names/EmbeddedFiles/Names")
    leaf = doc.get_new_xref()
    doc.update_object(leaf, f"<</Limits [(file0) (file1)] /Names {names}>>")
    root = doc.get_new_xref()
    doc.update_object(root, f"<</Kids [{leaf} 0 R {leaf} 0 R]>>")  # the same leaf listed twice
    doc.xref_set_key(cat, "Names/EmbeddedFiles", f"{root} 0 R")
    doc[0].add_file_annot((72, 100), b"Signed by the landlord's agent.", "agent.txt")
    pdf = tmp_path / "kids.pdf"
    doc.save(str(pdf))
    doc.close()
    ex = extract.extract(pdf)
    assert ex.attachments == 3 and ex.attachments_skipped == ex.attachments_failed == 0
    assert CLIENT in ex.text and CLIENT_EMAIL in ex.text and "landlord" in ex.text


def test_attachment_type_comes_from_any_of_its_names_or_its_content(tmp_path):
    import pymupdf

    inner = _pdf_bytes(f"Exhibit A: a letter to {CLIENT} about the lease.")
    # No extension, and a line break before the %PDF- header (readers accept both).
    doc = pymupdf.open(stream=_pdf_bytes("Cover sheet for the tenant's bundle of letters.",
                                         [("Exhibit A", b"\r\n" + inner)]))
    doc.embfile_add("phone", f"Phone {CLIENT_EMAIL}.".encode(), filename="note.txt", ufilename="Phone note")
    pdf = tmp_path / "names.pdf"
    doc.save(str(pdf))
    doc.close()
    ex = extract.extract(pdf)
    assert ex.attachments == 2 and ex.attachments_skipped == ex.attachments_failed == 0 and ex.pages == 2
    assert "exhibit a" in ex.text.lower() and CLIENT_EMAIL in ex.text


def test_attachments_nested_too_deep_are_counted(tmp_path):
    data = _pdf_bytes("Level 4 of the nested bundle of letters.")
    for level in (3, 2, 1, 0):
        data = _pdf_bytes(f"Level {level} of the nested bundle of letters.", [(f"level {level + 1}.pdf", data)])
    pdf = tmp_path / "nested.pdf"
    pdf.write_bytes(data)
    ex = extract.extract(pdf)
    assert ex.attachments == 3 and ex.attachments_failed == 1 and ex.pages == 4
    assert "level 3 of" in ex.text.lower() and "level 4 of" not in ex.text.lower()


def test_attached_files_share_one_budget_across_levels(tmp_path, monkeypatch):
    # Nested copies can multiply to millions of reads from a file of a few KB.
    monkeypatch.setattr(extract, "MAX_ATTACHMENTS", 3)
    inner = _pdf_bytes("Inner letter of the nested bundle of letters.", [("a.txt", b"one"), ("b.txt", b"two")])
    pdf = tmp_path / "budget.pdf"
    pdf.write_bytes(_pdf_bytes("Cover sheet for the tenant's bundle of letters.",
                               [("inner.pdf", inner), ("c.txt", b"three")]))
    ex = extract.extract(pdf)
    assert ex.attachments == 3 and ex.attachments_failed == 1


def test_attached_image_counts_as_embedded_image_ocr(tmp_path):
    # Not as an "image file": there is none in the matter. Its text is marked as OCR.
    pdf = tmp_path / "photo.pdf"
    png = render_png(tmp_path / "shot.png", [f"Hi {CLIENT}"]).read_bytes()
    pdf.write_bytes(_pdf_bytes("Cover sheet for the tenant's photo of the notice.", [("photo.png", png)]))
    ex = extract.extract(pdf)
    assert ex.attachments == 1 and ex.ocr_regions == 1 and ex.images_ocr == 0
    assert "[OCR]" in ex.text and "fakerton" in ex.text.lower()


def test_eml_attachment_names_have_underscores_split_like_file_names(tmp_path):
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"], msg["Subject"] = "support@example.org", "Statement"
    msg.set_content("See attached.")
    msg.add_attachment(b"%PDF-1.7", maintype="application", subtype="pdf",
                       filename=CLIENT.replace(" ", "_") + "_statement.pdf")
    eml = tmp_path / "mail.eml"
    eml.write_bytes(bytes(msg))
    ex = extract.extract(eml)
    assert f"{CLIENT} statement.pdf" in ex.text and ex.attachments_skipped == 1


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
