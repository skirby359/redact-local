"""Where the output goes (<matter>\\redacted), and the version shown with it. Synthetic data only."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from conftest import CLIENT, CONFIG, LETTER, assert_no_secrets, cli, out_dir, outputs, run

from redact_local import __version__
from redact_local.errors import SafeError


def test_output_goes_inside_the_matter_and_is_never_read_back(matter):
    (matter / "letter.txt").write_text(LETTER, encoding="utf-8")
    first = run(matter)
    assert first.out_dir == matter / "redacted"
    assert list(outputs(matter)) == ["letter.txt.txt"]
    # A rerun must not treat its own output as input.
    second = run(matter)
    assert second.summary["files"]["processed"] == 1
    assert list(outputs(matter)) == ["letter.txt.txt"]
    manifest = json.loads((matter / "_redact" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["out_dir"] == "redacted"


def test_cli_prints_the_folder_it_writes_and_reports_the_old_one(matter, tmp_path):
    (matter / "letter.txt").write_text(LETTER, encoding="utf-8")
    # A pre-0.2.0 matter: output beside the matter, and a manifest that points into it.
    legacy = matter.parent / f"{matter.name} - redacted"
    legacy.mkdir()
    (legacy / "letter.txt.txt").write_text("old output", encoding="utf-8")
    (matter / "_redact" / "manifest.json").write_text(json.dumps({"outputs": {"letter.txt": "letter.txt.txt"}}),
                                                      encoding="utf-8")

    proc = cli("run", str(matter), "--config", str(CONFIG), cwd=tmp_path)
    console = proc.stdout + proc.stderr
    assert proc.returncode == 0, console
    assert_no_secrets(console)
    assert proc.stdout.startswith(f"redact-local {__version__}")
    printed = re.search(r"^Output folder: (.+)$", proc.stdout, re.M).group(1).strip()
    assert Path(printed) == out_dir(matter)
    assert (Path(printed) / "_summary.json").is_file() and (Path(printed) / "letter.txt.txt").is_file()
    assert f"no longer updated: {legacy}" in proc.stdout
    assert (legacy / "letter.txt.txt").read_text(encoding="utf-8") == "old output"


def test_a_users_own_redacted_folder_is_never_written_into(matter, tmp_path):
    # Lower case so the name matches on case-sensitive file systems too.
    own = matter / "redacted"
    own.mkdir()
    (own / "court copy.txt").write_text(LETTER, encoding="utf-8")
    proc = cli("run", str(matter), "--config", str(CONFIG), cwd=tmp_path)
    console = proc.stdout + proc.stderr
    assert proc.returncode == 1, console
    assert_no_secrets(console)
    assert "already has something named 'redacted'" in proc.stderr
    assert sorted(p.name for p in own.iterdir()) == ["court copy.txt"]


def test_running_on_the_output_folder_is_refused(matter):
    (matter / "letter.txt").write_text(LETTER, encoding="utf-8")
    run(matter)
    for target in (out_dir(matter), out_dir(matter) / "letter.txt.txt"):
        with pytest.raises(SafeError):
            run(matter, target=target)


def test_review_sheet_summary_and_outputs_show_the_version(matter):
    (matter / "letter.txt").write_text(f"Dear {CLIENT}.", encoding="utf-8")
    result = run(matter)
    assert f"redact-local {__version__}" in result.review.read_text(encoding="utf-8")
    assert result.summary["version"] == __version__ and result.summary["build"].startswith(__version__)
    assert outputs(matter)["letter.txt.txt"].startswith(f"[redact-local {__version__}")
