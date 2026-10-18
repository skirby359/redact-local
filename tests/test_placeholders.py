"""Placeholders stay consistent across files and reruns; file names are redacted."""

from __future__ import annotations

import re

from conftest import OTHER, out_dir, outputs, run

PH = re.compile(r"\[[A-Z][A-Z0-9-]*\]")


def _write(matter, name, text):
    (matter / name).write_text(text, encoding="utf-8")


def test_consistent_across_files_and_reruns(matter):
    _write(matter, "a.txt", f"I met {OTHER} and wrote to rfakename@example.net. Then Quentin Fakerton called.")
    _write(matter, "b.txt", f"Later {OTHER} replied from rfakename@example.net. Robert was polite.")
    run(matter)
    first = outputs(matter)
    a, b = first["a.txt.txt"], first["b.txt.txt"]
    assert "[PERSON-1]" in a and "[PERSON-1]" in b
    assert "[EMAIL-1]" in a and "[EMAIL-1]" in b
    assert b.count("[PERSON-1]") == 2  # "Robert" alone links to "Robert Fakename"

    # Rerun: identical output.
    run(matter)
    assert outputs(matter) == first

    # A new file with a new person: old placeholders keep their numbers.
    _write(matter, "c.txt", f"Ophelia Nonperson spoke to {OTHER}.")
    run(matter)
    again = outputs(matter)
    assert again["a.txt.txt"] == a and again["b.txt.txt"] == b
    assert "[PERSON-2]" in again["c.txt.txt"] and "[PERSON-1]" in again["c.txt.txt"]


def test_single_file_run_uses_the_matter_key(matter):
    _write(matter, "a.txt", f"I met {OTHER}.")
    run(matter)
    _write(matter, "d.txt", f"{OTHER} again.")
    run(matter, target=matter / "d.txt")
    outs = outputs(matter)
    assert "[PERSON-1]" in outs["d.txt.txt"]
    assert "a.txt.txt" in outs  # the single-file run didn't remove other outputs


def test_file_names_are_redacted(matter):
    _write(matter, "Quentin_Fakerton letter to Marisol Testwell.txt", "Body text.")
    sub = matter / "Fakerton correspondence"
    sub.mkdir()
    _write(sub, "reply from qfakerton.md", "Body.")
    run(matter)
    names = list(outputs(matter))
    joined = " ".join(names).lower()
    for leaked in ("quentin", "fakerton", "marisol", "testwell", "qfakerton"):
        assert leaked not in joined
    assert "[CLIENT] letter to [MANAGER].txt.txt" in names
    assert any(n.startswith("[CLIENT] correspondence") and "[CLIENT-HANDLE]" in n for n in names)


def test_renamed_output_replaces_the_stale_one(matter):
    _write(matter, "Ophelia Nonperson memo.txt", "Body.")
    run(matter)
    before = set(outputs(matter))
    (matter / "_redact" / "names.txt").write_text("Ophelia Nonperson => WITNESS\n", encoding="utf-8")
    run(matter)
    after = set(outputs(matter))
    assert after == {"[WITNESS] memo.txt.txt"}
    assert not any("Ophelia" in n for n in after)
    assert before != after or "[WITNESS]" in next(iter(before))


def test_only_text_is_written(matter):
    from conftest import render_png

    render_png(matter / "shot.png", ["Hello there"])
    _write(matter, "note.txt", "Hi.")
    _write(matter, "data.xlsx", "not really a spreadsheet")
    result = run(matter)
    files = sorted(p.name for p in out_dir(matter).rglob("*") if p.is_file())
    assert files == ["_summary.json", "note.txt.txt", "shot.png.txt"]
    assert result.summary["files"]["skipped"] == 1
