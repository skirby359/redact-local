"""Regression tests for leaks and bugs found in review. Synthetic data only."""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

import pytest
from conftest import CLIENT, CLIENT_EMAIL, CONFIG, NAMES_TXT, OTHER, assert_no_secrets, cli, out_dir, outputs, run

from redact_local import detect, restore
from redact_local.errors import SafeError


def _only_output(matter) -> str:
    outs = outputs(matter)
    assert len(outs) == 1, list(outs)
    return next(iter(outs.values()))


def _names(matter, body: str) -> None:
    (matter / "_redact" / "names.txt").write_text(body, encoding="utf-8")


# ── extraction ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-8-sig"])
def test_utf16_and_bom_text_files_are_decoded(matter, encoding):
    # "utf-16" writes a BOM; "-le"/"-be" don't (PowerShell 5's > writes UTF-16 with a BOM).
    (matter / "note.txt").write_bytes(f"Letter for {CLIENT} at {CLIENT_EMAIL}.".encode(encoding))
    run(matter)
    out = _only_output(matter)
    assert "fakerton" not in out.lower() and "[CLIENT]" in out


def test_html_only_email_cells_dont_fuse(matter):
    html = (f"<table><tr><td>Account</td><td>qfakerton</td></tr><tr><td>Contact</td><td>{CLIENT}</td></tr></table>"
            "<p>Thanks</p><p>2019</p>")
    (matter / "mail.eml").write_text(
        "From: support@example.org\nTo: someone@example.org\nSubject: Account\n"
        f"Content-Type: text/html\n\n{html}\n", encoding="utf-8")
    run(matter)
    out = _only_output(matter)
    assert "fakerton" not in out.lower()
    assert re.search(r"Account\s+\[CLIENT-HANDLE\]", out) and re.search(r"Contact\s+\[CLIENT\]", out)


def test_transparent_png_is_read(tmp_path):
    from PIL import Image, ImageDraw

    from conftest import font
    from redact_local import extract

    img = Image.new("RGBA", (900, 160), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((30, 50), f"Signed {CLIENT}", fill=(0, 0, 0, 255), font=font(48))
    img.save(tmp_path / "t.png")
    assert "fakerton" in extract.extract(tmp_path / "t.png").text.lower()


def test_crlf_sources_dont_double_line_endings(matter):
    (matter / "a.txt").write_bytes(b"line one\r\nline two\r\n")
    run(matter)
    raw = (out_dir(matter) / "a.txt.txt").read_bytes()
    assert b"\r\r\n" not in raw


# ── names.txt matching ─────────────────────────────────────────────────────

def test_listed_names_next_to_digits_underscores_and_plurals(matter):
    text = ("Attachment: Quentin_Fakerton_resume.pdf. The Fakertons replied. FAKERTON2 signed. "
            "Old handle qfakerton_old. Link: /search?q=%20Fakerton. Not Fakertonville.")
    (matter / "a.txt").write_text(text, encoding="utf-8")
    run(matter)
    out = _only_output(matter)
    assert out.lower().count("fakerton") == 1 and "Fakertonville" in out


def test_file_names_with_camelcase_and_digits(matter):
    (matter / "FakertonLetter.txt").write_text("Body.", encoding="utf-8")
    (matter / "Fakerton2.txt").write_text("Body.", encoding="utf-8")
    run(matter)
    names = " ".join(outputs(matter)).lower()
    assert "fakerton" not in names


def test_email_attachment_names_are_analyzed_like_file_names(matter):
    # "RobertFakename2024.docx" is one token to the model; split, it's a person. A names.txt
    # entry still matches the name as written ("DelphineMcFakerton"), so nothing is lost.
    from email.message import EmailMessage

    _names(matter, NAMES_TXT + "Delphine McFakerton => WITNESS\n")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "support@example.org", "someone@example.org", "Statements"
    msg.set_content("See attached.")
    for fn in (OTHER.replace(" ", "") + "2024.docx", "DelphineMcFakerton_statement.pdf"):
        msg.add_attachment(b"x", maintype="application", subtype="octet-stream", filename=fn)
    (matter / "mail.eml").write_bytes(bytes(msg))
    run(matter)
    out = _only_output(matter)
    assert "fakename" not in out.lower() and "mcfakerton" not in out.lower()
    assert "[PERSON-" in out and "[WITNESS]" in out


def test_name_suffixes_last_first_entries_and_hyphenated_surnames(matter):
    _names(matter, "Quentin Fakerton Jr. => CLIENT\nTestwell-Smith, Marisol => WITNESS\n")
    (matter / "a.txt").write_text(
        "MR. FAKERTON wrote. The Jr. varsity met in Division III. "
        "TESTWELL and TESTWELL SMITH and Marisol Testwell-Smith attended.", encoding="utf-8")
    run(matter)
    out = _only_output(matter)
    assert "FAKERTON" not in out and "TESTWELL" not in out.upper()
    # The suffix isn't a name variant: "Jr." and "III" aren't taken as [CLIENT].
    assert out.count("[CLIENT]") == 1 and "Division III" in out


@pytest.mark.parametrize("variant", [
    "O’Fakerton",            # curly apostrophe vs straight in names.txt
    "QUENTIN FAKER­TON",     # soft hyphen
    "Ｆａｋｅｒｔｏｎ",                # fullwidth letters
    "Faker-\nton",               # hyphenated across a line break
    "Faker​ton",            # zero-width space
])
def test_unicode_variants_are_normalized(matter, variant):
    _names(matter, "Quentin O'Fakerton => CLIENT\n" if "O" in variant[:2] else "Quentin Fakerton => CLIENT\n")
    (matter / "a.txt").write_text(f"Signed by {variant} today.", encoding="utf-8")
    run(matter)
    out = _only_output(matter)
    assert "faker" not in out.lower().replace("[client]", ""), out


ARIAL = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"


@pytest.mark.skipif(not ARIAL.is_file(), reason="needs a font with ligature glyphs")
def test_ligatures_in_pdf_text(matter):
    import pymupdf

    _names(matter, "Grace Griffin => CLIENT\n")
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Letter to Grace Gri\ufb03n about the lease and the deposit.",
                               fontsize=12, fontname="arialx", fontfile=str(ARIAL))
    doc.save(str(matter / "a.pdf"))
    doc.close()
    run(matter)
    assert "gri" not in _only_output(matter).lower()


def test_role_numbers_are_stable_when_names_txt_grows(matter, tmp_path):
    _names(matter, "Pat Staffer => STAFF\nSam Staffer => STAFF\n")
    (matter / "a.txt").write_text("Pat Staffer and Sam Staffer met.", encoding="utf-8")
    run(matter)
    first = _only_output(matter)
    assert "[STAFF-1] and [STAFF-2]" in first
    draft = tmp_path / "d.txt"
    draft.write_text("[STAFF-1] then [STAFF-2].", encoding="utf-8")

    _names(matter, "Alex Staffer => STAFF\nPat Staffer => STAFF\nSam Staffer => STAFF\n")
    (matter / "b.txt").write_text("Alex Staffer joined.", encoding="utf-8")
    run(matter)
    assert outputs(matter)["a.txt.txt"] == first
    assert "[STAFF-3]" in outputs(matter)["b.txt.txt"]
    text = restore.restore(draft, matter).output.read_text(encoding="utf-8")
    assert text == "Pat Staffer then Sam Staffer."


def test_single_role_keeps_its_placeholder_when_shared_later(matter):
    _names(matter, "Pat Staffer => STAFF\n")
    (matter / "a.txt").write_text("Pat Staffer met.", encoding="utf-8")
    run(matter)
    assert "[STAFF] met" in _only_output(matter)
    _names(matter, "Pat Staffer => STAFF\nSam Staffer => STAFF\n")
    (matter / "b.txt").write_text("Sam Staffer met.", encoding="utf-8")
    run(matter)
    outs = outputs(matter)
    assert "[STAFF] met" in outs["a.txt.txt"] and "[STAFF-2] met" in outs["b.txt.txt"]


@pytest.mark.parametrize("role", ["PERSON", "person 2", "2nd witness", "CLIENT-EMAIL", "address"])
def test_reserved_or_unrestorable_roles_are_refused(matter, role):
    _names(matter, f"Quentin Fakerton => {role}\n")
    (matter / "a.txt").write_text("Hello.", encoding="utf-8")
    with pytest.raises(SafeError) as info:
        run(matter)
    assert "line 1" in str(info.value) and "Fakerton" not in str(info.value)


# ── detection ──────────────────────────────────────────────────────────────

def test_long_text_chunks_never_split_an_email(matter, monkeypatch):
    monkeypatch.setattr(detect, "_CHUNK", 300)
    filler = "lorem ipsum " * 23  # ~276 chars, so the email straddles the 300 mark
    (matter / "a.txt").write_text(filler + "write to ophelia.nonperson@example.net today " + filler, encoding="utf-8")
    run(matter)
    out = _only_output(matter)
    assert "ophelia" not in out and "[EMAIL-1]" in out


def test_names_inside_a_court_parenthetical_are_redacted(matter):
    text = "See 123 Wn.2d 456 (testimony of Quentin Fakerton, 2019) and State v. Doe, 1 Wn.2d 2 (1990)."
    for profile in ("client", "court-records"):
        (matter / "a.txt").write_text(text, encoding="utf-8")
        run(matter, profile)
        out = _only_output(matter)
        assert "Fakerton" not in out, profile
        assert "123 Wn.2d 456" in out and "1 Wn.2d 2 (1990)" in out


def test_emails_at_private_domains(matter):
    (matter / "a.txt").write_text("Write to ophelia@corp.local or ops@intranet.internal.", encoding="utf-8")
    run(matter)
    out = _only_output(matter)
    assert "ophelia" not in out and "ops@" not in out


# ── pipeline ───────────────────────────────────────────────────────────────

def test_subfolder_run_uses_the_matter(matter):
    sub = matter / "Fakerton emails"
    sub.mkdir()
    (sub / "a.txt").write_text(f"From {CLIENT}.", encoding="utf-8")
    (matter / "top.txt").write_text(f"About {OTHER}.", encoding="utf-8")
    run(matter)
    (sub / "b.txt").write_text(f"Again {CLIENT} and {OTHER}.", encoding="utf-8")
    result = run(matter, target=sub)
    assert result.matter == matter and result.out_dir == out_dir(matter)
    assert not (sub / "_redact").exists()
    assert not (matter / "Fakerton emails - redacted").exists()
    outs = outputs(matter)
    assert "top.txt.txt" in outs  # the subfolder run left other outputs alone
    assert all("fakerton" not in name.lower() for name in outs)
    b = next(text for name, text in outs.items() if name.endswith("b.txt.txt"))
    assert "[CLIENT]" in b and "[PERSON-1]" in b


@pytest.mark.skipif(sys.platform != "win32", reason="read-only files can be deleted on POSIX")
def test_locked_stale_output_is_reported_not_fatal(matter):
    (matter / "Ophelia Nonperson memo.txt").write_text("Body.", encoding="utf-8")
    run(matter)
    stale = out_dir(matter) / next(iter(outputs(matter)))
    os.chmod(stale, stat.S_IREAD)
    try:
        _names(matter, "Ophelia Nonperson => WITNESS\n")
        result = run(matter)
        assert result.summary["stale_outputs_not_removed"] == 1
        assert "[WITNESS] memo.txt.txt" in outputs(matter)
    finally:
        os.chmod(stale, stat.S_IREAD | stat.S_IWRITE)


# ── restore ────────────────────────────────────────────────────────────────

def test_docx_restore_inside_hyperlinks(matter, tmp_path):
    import docx
    from docx.opc.constants import RELATIONSHIP_TYPE
    from docx.oxml.shared import OxmlElement, qn

    (matter / "a.txt").write_text(f"Letter to {CLIENT}.", encoding="utf-8")
    run(matter)
    d = docx.Document()
    p = d.add_paragraph("Email ")
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), d.part.relate_to("mailto:x@example.org", RELATIONSHIP_TYPE.HYPERLINK, is_external=True))
    r, t = OxmlElement("w:r"), OxmlElement("w:t")
    t.text = "[CLIENT]"
    r.append(t)
    link.append(r)
    p._p.append(link)
    p.add_run(" today.")
    d.save(str(tmp_path / "draft.docx"))
    result = restore.restore(tmp_path / "draft.docx", matter)
    assert docx.Document(str(result.output)).paragraphs[0].text == f"Email {CLIENT} today."
    assert result.replaced == 1


# ── console ────────────────────────────────────────────────────────────────

def test_cli_handles_non_ascii_paths(tmp_path):
    m = tmp_path / "Matter Łódź 003"
    (m / "_redact").mkdir(parents=True)
    (m / "a.txt").write_text(f"Hi {CLIENT}.", encoding="utf-8")
    proc = cli("run", str(m), "--config", str(CONFIG), cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert_no_secrets(proc.stdout + proc.stderr)
