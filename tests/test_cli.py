"""The CLI never prints document text or detected values, on success or failure."""

from __future__ import annotations

import json

from conftest import CLIENT, CONFIG, LETTER, OTHER, assert_no_secrets, cli, make_portfolio, out_dir, outputs, render_png


def _populate(matter, tmp_path):
    (matter / "Quentin_Fakerton letter.txt").write_text(LETTER + "\nZorbleflax was cc'd.\n", encoding="utf-8")
    render_png(matter / "Marisol Testwell screenshot.png", [f"Hi {CLIENT}", "your rent for @qfakerton is overdue"])
    # A broken PDF named after a fake person: the failure must not echo the name.
    (matter / "Robert Fakename broken.pdf").write_bytes(b"%PDF-1.7 not really a pdf Fakerton 219-44-6012")
    # A portfolio whose embedded files are named after the client.
    make_portfolio(matter / "Quentin Fakerton portfolio.pdf", tmp_path)


def test_run_and_restore_print_counts_and_paths_only(matter, tmp_path):
    _populate(matter, tmp_path)
    proc = cli("run", str(matter), "--config", str(CONFIG), cwd=tmp_path)
    console = proc.stdout + proc.stderr
    assert_no_secrets(console)
    assert proc.returncode == 4, console          # one file failed, others written
    assert "Output folder:" in proc.stdout and "Review sheet:" in proc.stdout
    assert "Network guard: on, 0 attempts blocked" in proc.stdout
    assert "2 files attached to PDFs" in proc.stdout and "Attachments not opened: 1" in proc.stdout

    summary = json.loads((out_dir(matter) / "_summary.json").read_text(encoding="utf-8"))
    assert summary["files"]["processed"] == 3 and summary["files"]["failed"] == 1
    assert_no_secrets(json.dumps(summary))

    draft = tmp_path / "draft.txt"
    draft.write_text("Dear [CLIENT], see [PERSON-1].", encoding="utf-8")
    proc = cli("restore", str(draft), "--matter", str(matter), cwd=tmp_path)
    console = proc.stdout + proc.stderr
    assert proc.returncode == 0, console
    assert_no_secrets(console)
    assert "Restored 2 placeholders" in proc.stdout
    assert CLIENT in (tmp_path / "draft - restored.txt").read_text(encoding="utf-8")


def test_unreadable_attachment_makes_the_run_partial(matter, tmp_path):
    # Exit 4 tells the skill some content is missing; a locked attachment is missing content.
    import pymupdf

    locked = pymupdf.open()
    locked.new_page().insert_text((72, 72), f"Locked letter to {CLIENT} about the lease.", fontsize=11)
    data = locked.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    locked.close()
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Cover sheet for the tenant's bundle of letters.", fontsize=11)
    doc.embfile_add("locked", data, filename=f"{CLIENT} locked.pdf")
    doc.save(str(matter / "bundle.pdf"))
    doc.close()
    proc = cli("run", str(matter), "--config", str(CONFIG), cwd=tmp_path)
    console = proc.stdout + proc.stderr
    assert_no_secrets(console)
    assert proc.returncode == 4, console
    assert "1 file attached to a PDF could not be read" in proc.stdout


def test_suggested_names_are_reported_as_a_count_only(tmp_path):
    m = tmp_path / "Matter 005"
    m.mkdir()
    (m / "letter.txt").write_text(LETTER + "\nZorbleflax was cc'd.\n", encoding="utf-8")
    proc = cli("run", str(m), "--config", str(CONFIG), cwd=tmp_path)
    console = proc.stdout + proc.stderr
    assert proc.returncode == 0, console
    assert_no_secrets(console)
    assert "the scan found, commented out." in proc.stdout
    names_txt = (m / "_redact" / "names.txt").read_text(encoding="utf-8")
    assert "# Robert Fakename =>" in names_txt

    proc = cli("run", str(m), "--config", str(CONFIG), "--suggest-names", cwd=tmp_path)
    console = proc.stdout + proc.stderr
    assert proc.returncode == 0, console
    assert_no_secrets(console)
    assert "names.txt: no new people to suggest." in proc.stdout


def test_roles_command_prints_counts_only_and_the_rerun_uses_the_role(tmp_path):
    m = tmp_path / "Matter 007"
    m.mkdir()
    (m / "letter.txt").write_text(f"I spoke with {OTHER} yesterday about the appeal.", encoding="utf-8")
    assert cli("run", str(m), "--config", str(CONFIG), cwd=tmp_path).returncode == 0

    proc = cli("roles", str(m), "PERSON-1=WITNESS", cwd=tmp_path)
    console = proc.stdout + proc.stderr
    assert proc.returncode == 0, console
    assert_no_secrets(console)
    assert "1 role set" in proc.stdout

    proc = cli("roles", str(m), "PERSON-7=OWNER", cwd=tmp_path)
    assert proc.returncode == 2 and "PERSON-7" in proc.stderr
    assert_no_secrets(proc.stdout + proc.stderr)

    assert cli("run", str(m), "--config", str(CONFIG), cwd=tmp_path).returncode == 0
    text = next(iter(outputs(m).values()))
    assert "[WITNESS]" in text and "Fakename" not in text


def test_errors_are_safe(matter, tmp_path):
    proc = cli("run", str(tmp_path / "Quentin Fakerton missing"), cwd=tmp_path)
    assert proc.returncode == 1
    assert "Input path not found." in proc.stderr
    assert_no_secrets(proc.stdout + proc.stderr)

    proc = cli("run", str(matter), "--profile", "nope", "--config", str(CONFIG), cwd=tmp_path)
    assert proc.returncode == 2 and "Unknown profile" in proc.stderr
