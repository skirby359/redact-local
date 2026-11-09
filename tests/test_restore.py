"""restore: placeholders back to real values, for .txt/.md and .docx drafts."""

from __future__ import annotations

import re
import shutil

import pytest
from conftest import CARD, CLIENT, CLIENT_EMAIL, CONFIG, LETTER, OTHER, SSN, STAFF, out_dir, outputs, run

from redact_local import restore
from redact_local.errors import UsageError

FAKE_DL = "WDLFAKE0X12Q"


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


# ── Court copies (restore --court) ─────────────────────────────────────────

def _court_matter(matter):
    """A letter with an SSN, a card number and a WA driver licence; returns its placeholders."""
    (matter / "letter.txt").write_text(LETTER + f"Driver license {FAKE_DL}.\n", encoding="utf-8")
    run(matter)
    text = next(iter(outputs(matter).values()))
    find = lambda prefix: re.search(rf"\[({prefix}-\d+)\]", text).group(1)  # noqa: E731
    return find("SSN"), find("CARD"), find("DRIVER-LICENSE")


def test_mask_last4_keeps_separators_and_the_last_four():
    assert restore.mask_last4(SSN) == "XXX-XX-6012"
    assert restore.mask_last4(CARD) == "XXXX XXXX XXXX 1111"
    assert restore.mask_last4("GB82 WEST 1234 5698 7654 32") == "XXXX XXXX XXXX XXXX XX54 32"
    assert restore.mask_last4("1234") == "XXXX"     # four or fewer: nothing left to show


def test_court_copy_cuts_ids_to_last_four_and_withholds_licences(matter, tmp_path):
    ssn, card, dl = _court_matter(matter)
    draft = tmp_path / "declaration.md"
    draft.write_text(f"I, [CLIENT] ([CLIENT-EMAIL]), SSN [{ssn}], card [{card}], license [{dl}].\n",
                     encoding="utf-8")
    result = restore.restore(draft, matter, court=True)
    assert result.output == tmp_path / "declaration - court copy.md"
    text = result.output.read_text(encoding="utf-8")
    assert CLIENT in text and CLIENT_EMAIL in text
    assert "XXX-XX-6012" in text and SSN not in text
    assert "XXXX XXXX XXXX 1111" in text and CARD not in text
    assert "license [REDACTED]" in text and FAKE_DL not in text
    assert (result.masked, result.withheld, result.unknown) == (2, 1, 0)
    # The ordinary restore is unchanged: everything comes back in full.
    plain = restore.restore(draft, matter).output.read_text(encoding="utf-8")
    assert SSN in plain and CARD in plain and FAKE_DL in plain


def test_court_copy_docx_with_placeholder_split_across_runs(matter, tmp_path):
    import docx

    ssn, _card, dl = _court_matter(matter)
    d = docx.Document()
    p = d.add_paragraph()
    p.add_run(f"SSN [{ssn[:2]}")
    p.add_run(f"{ssn[2:]}] and license [{dl}] for [CLIENT].")
    draft = tmp_path / "declaration.docx"
    d.save(str(draft))
    result = restore.restore(draft, matter, court=True)
    text = docx.Document(str(result.output)).paragraphs[0].text
    assert text == f"SSN XXX-XX-6012 and license [REDACTED] for {CLIENT}."
    assert (result.masked, result.withheld, result.unknown) == (1, 1, 0)


def _config_with(tmp_path, court_filing: str):
    cfg = tmp_path / "config"
    shutil.copytree(CONFIG, cfg)
    toml = (cfg / "profiles.toml").read_text(encoding="utf-8")
    toml = toml[: toml.index("[court_filing]")] + court_filing
    (cfg / "profiles.toml").write_text(toml, encoding="utf-8")
    return cfg


def test_court_copy_rules_come_from_profiles_toml(matter, tmp_path):
    ssn, card, dl = _court_matter(matter)
    cfg = _config_with(tmp_path, '[court_filing]\nlast4 = ["DRIVER-LICENSE"]\nwithhold = ["SSN", "CARD"]\n'
                                 'withheld_text = "[omitted per GR 31]"\n')
    draft = tmp_path / "d.txt"
    draft.write_text(f"[{ssn}] [{card}] [{dl}]", encoding="utf-8")
    text = restore.restore(draft, matter, court=True, config=cfg).output.read_text(encoding="utf-8")
    assert text == "[omitted per GR 31] [omitted per GR 31] XXXXXXXXX12Q"


@pytest.mark.parametrize("table, message", [
    ('[court_filing]\nlast4 = ["SSN"]\nwithhold = ["SSN"]\n', "both last4 and withhold"),
    ('[court_filing]\nlast4 = ["BANANA"]\n', "unknown categories"),
    ('[court_filing]\nwithheld_text = "  "\n', "must not be empty"),
])
def test_court_filing_config_errors(matter, tmp_path, table, message):
    _court_matter(matter)
    draft = tmp_path / "d.txt"
    draft.write_text("[CLIENT]", encoding="utf-8")
    with pytest.raises(UsageError, match=message):
        restore.restore(draft, matter, court=True, config=_config_with(tmp_path, table))
