"""The CLI never prints document text or detected values, on success or failure."""

from __future__ import annotations

import json

from conftest import CLIENT, CONFIG, LETTER, assert_no_secrets, cli, out_dir, render_png


def _populate(matter):
    (matter / "Quentin_Fakerton letter.txt").write_text(LETTER + "\nZorbleflax was cc'd.\n", encoding="utf-8")
    render_png(matter / "Marisol Testwell screenshot.png", [f"Hi {CLIENT}", "your rent for @qfakerton is overdue"])
    # A broken PDF named after a fake person: the failure must not echo the name.
    (matter / "Robert Fakename broken.pdf").write_bytes(b"%PDF-1.7 not really a pdf Fakerton 219-44-6012")


def test_run_and_restore_print_counts_and_paths_only(matter, tmp_path):
    _populate(matter)
    proc = cli("run", str(matter), "--config", str(CONFIG), cwd=tmp_path)
    console = proc.stdout + proc.stderr
    assert_no_secrets(console)
    assert proc.returncode == 4, console          # one file failed, others written
    assert "Output folder:" in proc.stdout and "Review sheet:" in proc.stdout
    assert "Network guard: on, 0 attempts blocked" in proc.stdout

    summary = json.loads((out_dir(matter) / "_summary.json").read_text(encoding="utf-8"))
    assert summary["files"]["processed"] == 2 and summary["files"]["failed"] == 1
    assert_no_secrets(json.dumps(summary))

    draft = tmp_path / "draft.txt"
    draft.write_text("Dear [CLIENT], see [PERSON-1].", encoding="utf-8")
    proc = cli("restore", str(draft), "--matter", str(matter), cwd=tmp_path)
    console = proc.stdout + proc.stderr
    assert proc.returncode == 0, console
    assert_no_secrets(console)
    assert "Restored 2 placeholders" in proc.stdout
    assert CLIENT in (tmp_path / "draft - restored.txt").read_text(encoding="utf-8")


def test_errors_are_safe(matter, tmp_path):
    proc = cli("run", str(tmp_path / "Quentin Fakerton missing"), cwd=tmp_path)
    assert proc.returncode == 1
    assert "Input path not found." in proc.stderr
    assert_no_secrets(proc.stdout + proc.stderr)

    proc = cli("run", str(matter), "--profile", "nope", "--config", str(CONFIG), cwd=tmp_path)
    assert proc.returncode == 2 and "Unknown profile" in proc.stderr
