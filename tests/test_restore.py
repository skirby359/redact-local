"""restore: placeholders back to real values, for .txt/.md and .docx drafts."""

from __future__ import annotations

from conftest import CLIENT, CLIENT_EMAIL, LETTER, OTHER, STAFF, out_dir, outputs, run

from redact_local import restore


def test_round_trip_txt(matter, tmp_path):
    (matter / "letter.txt").write_text(LETTER, encoding="utf-8")
    run(matter)
    redacted = next(iter(outputs(matter).values()))
    assert CLIENT not in redacted
    # A draft Claude might write, using only placeholders.
    draft = tmp_path / "draft.md"
    draft.write_text("Dear [CLIENT],\n\n[MANAGER] wrote to [CLIENT-EMAIL]. [PERSON-1] agreed. [NOPE-9] stays.\n",
                     encoding="utf-8")
    result = restore.restore(draft, matter)
    assert result.output == tmp_path / "draft - restored.md"
    text = result.output.read_text(encoding="utf-8")
    assert f"Dear {CLIENT}," in text and STAFF in text and CLIENT_EMAIL in text and OTHER in text
    assert "[NOPE-9]" in text
    assert (result.replaced, result.distinct, result.unknown) == (4, 4, 1)


def test_round_trip_of_the_redacted_output_itself(matter, tmp_path):
    (matter / "letter.txt").write_text(LETTER, encoding="utf-8")
    run(matter)
    draft = tmp_path / "copy.txt"
    draft.write_text(next(iter(outputs(matter).values())), encoding="utf-8")
    text = restore.restore(draft, matter).output.read_text(encoding="utf-8")
    assert CLIENT in text and OTHER in text and "(206) 555-0142" in text


def test_round_trip_docx_with_placeholder_split_across_runs(matter, tmp_path):
    import docx

    (matter / "letter.txt").write_text(LETTER, encoding="utf-8")
    run(matter)
    d = docx.Document()
    p = d.add_paragraph()
    p.add_run("Dear [CLI")
    p.add_run("ENT], thank you.")
    d.add_paragraph().add_run("Copy: [MANAGER]").bold = True
    draft = tmp_path / "draft.docx"
    d.save(str(draft))
    result = restore.restore(draft, matter)
    back = docx.Document(str(result.output))
    texts = [par.text for par in back.paragraphs]
    assert texts[0] == f"Dear {CLIENT}, thank you."
    assert texts[1] == f"Copy: {STAFF}" and back.paragraphs[1].runs[0].bold


def test_draft_inside_redacted_folder_restores_to_private_folder(matter):
    (matter / "letter.txt").write_text(LETTER, encoding="utf-8")
    run(matter)
    draft = out_dir(matter) / "draft.txt"
    draft.write_text("To [CLIENT].", encoding="utf-8")
    result = restore.restore(draft, out_dir(matter))  # --matter given as the redacted folder works too
    assert result.output.parent == matter / "_redact" / "restored"
    assert not (out_dir(matter) / "draft - restored.txt").exists()
