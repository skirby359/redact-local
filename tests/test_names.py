"""names.txt: parsing, roles, variants, case-insensitive matching."""

from __future__ import annotations

import pytest
from conftest import CLIENT_EMAIL, LETTER, OTHER, outputs, run

from redact_local import names
from redact_local.errors import UsageError


def _parse(tmp_path, body: str) -> names.NamesFile:
    p = tmp_path / "names.txt"
    p.write_text(body, encoding="utf-8")
    return names.parse(p)


def _matches(nf, text: str, variants=("full", "last-first", "initial-last", "first", "last"), ordinary=frozenset()):
    ms = names.build_matchers(nf, list(variants), lambda w: False, set(ordinary))
    return {text[h.start():h.end()] for m in ms for h in m.regex.finditer(text)}


def test_roles_aliases_and_numbering(tmp_path):
    nf = _parse(tmp_path, "Jane Q. Example | @jexample | jane@example.com => client\n"
                          "Pat Staffer => site manager\nSam Staffer => SITE-MANAGER\nNo Role Person\n# comment\n")
    assert [e.placeholder_base for e in nf.entries] == ["CLIENT", "SITE-MANAGER-1", "SITE-MANAGER-2", None]
    kinds = [a.kind for a in nf.entries[0].aliases]
    assert kinds == ["name", "handle", "email"]
    assert nf.entries[0].canonical == "Jane Q. Example"


def test_variants_match_case_insensitively(tmp_path):
    nf = _parse(tmp_path, "Jane Q. Example => CLIENT\n")
    text = ("JANE Q. EXAMPLE; Jane Example; Example, Jane; Example, Jane Q.; J. Example; "
            "jane_example; JaneExample; Hi Jane; Ms. Example")
    found = {m.casefold() for m in _matches(nf, text)}
    for expected in ["jane q. example", "jane example", "example, jane", "example, jane q.",
                     "j. example", "jane_example", "janeexample", "jane", "example"]:
        assert expected in found


def test_one_word_variant_that_is_an_ordinary_word_needs_a_capital(tmp_path):
    nf = _parse(tmp_path, "Grant Fakerton\n")
    found = _matches(nf, "Grant called. We grant the motion. GRANT FAKERTON.", ordinary={"grant"})
    assert "Grant" in found and "GRANT FAKERTON" in found
    assert "grant" not in found


def test_one_word_variant_on_keep_list_is_skipped(tmp_path):
    nf = _parse(tmp_path, "Jane Washington\n")
    ms = names.build_matchers(nf, ["first", "last"], lambda w: w.lower() == "washington", set())
    text = "Jane Washington lives in Washington."
    hits = sorted((h.start(), text[h.start():h.end()]) for m in ms for h in m.regex.finditer(text))
    assert (0, "Jane Washington") in hits
    assert not any(v == "Washington" and pos > 5 for pos, v in hits)


def test_handle_and_email_aliases(tmp_path):
    nf = _parse(tmp_path, "Jane Example | @jexample | jane@example.com\n")
    found = _matches(nf, "see @jexample and github.com/jexample, mail jane@example.com, not jexample@other.test")
    assert {"@jexample", "jexample", "jane@example.com"} <= found
    assert "jexample@other.test" not in found


def test_roles_in_pipeline_output(matter):
    (matter / "letter.txt").write_text(LETTER, encoding="utf-8")
    run(matter)
    text = next(iter(outputs(matter).values()))
    assert "[CLIENT]" in text and "[MANAGER]" in text
    assert "[CLIENT-EMAIL]" in text and "[CLIENT-HANDLE]" in text
    assert CLIENT_EMAIL not in text
    for leaked in ("Quentin", "Fakerton", "Marisol", "Testwell", "qfakerton"):
        assert leaked.lower() not in text.lower()


def test_template_created_when_missing(tmp_path):
    m = tmp_path / "Matter 002"
    m.mkdir()
    (m / "a.txt").write_text("Nothing personal here.", encoding="utf-8")
    result = run(m)
    assert result.names_created
    assert result.names_suggested == 0
    assert (m / "_redact" / "names.txt").read_text(encoding="utf-8").startswith("# names.txt")
    assert names.is_untouched_template(m / "_redact" / "names.txt")


# ── Suggested names ────────────────────────────────────────────────────────

def test_inline_comment_and_empty_role(tmp_path):
    nf = _parse(tmp_path, "Jane Example =>                 # [PERSON-1], 3 mentions in 2 files\n"
                          "Pat Staffer => staff   # [PERSON-2], 1 mention in 1 file\n"
                          "#   Sam Otherperson =>        # [PERSON-3], 1 mention in 1 file\n"
                          "Lee Witness | lee#1@example.com\n")
    assert [(e.canonical, e.placeholder_base) for e in nf.entries] == [
        ("Jane Example", None), ("Pat Staffer", "STAFF"), ("Lee Witness", None)]
    # A # with no space before it is part of the value, not a comment.
    assert [a.text for a in nf.entries[2].aliases] == ["Lee Witness", "lee#1@example.com"]


def test_suggestions_are_commented_out_and_never_repeated(tmp_path):
    p = tmp_path / "names.txt"
    names.write_template(p)
    first = [names.Suggestion("Jane Example", "PERSON-1", 3, 2), names.Suggestion("Example, Pat", "PERSON-2", 1, 1)]
    assert names.append_suggestions(p, first, "client") == 2
    assert not names.is_untouched_template(p)
    assert names.parse(p).entries == []                       # nothing active until uncommented
    body = p.read_text(encoding="utf-8")
    assert body.startswith(names.TEMPLATE)
    assert "# Jane Example =>" in body and "# [PERSON-1], 3 mentions in 2 files" in body
    assert "# [PERSON-2], 1 mention in 1 file" in body

    # Already on a line (commented or not, any case, either name order): not added again.
    again = [names.Suggestion("JANE EXAMPLE", "PERSON-1", 4, 2), names.Suggestion("Pat Example", "PERSON-2", 1, 1),
             names.Suggestion("Lee Witness", "PERSON-3", 1, 1), names.Suggestion("Lee Witness", "PERSON-4", 1, 1)]
    assert names.append_suggestions(p, again, "client") == 1
    body = p.read_text(encoding="utf-8")
    assert body.count(names.SUGGEST_HEADER) == 1 and body.count("# Lee Witness =>") == 1
    assert names.append_suggestions(p, again, "client") == 0


def test_clean_suggestion_drops_what_cannot_be_a_names_line():
    assert names.clean_suggestion("Jane  Example\n") == "Jane Example"
    assert names.clean_suggestion("Jane | Example => X # y") == "Jane Example X y"
    assert names.clean_suggestion("@jexample") == "jexample"
    for junk in ("Jr.", "van der", "J.", "12345", "jane@example.com"):
        assert names.clean_suggestion(junk) == "", junk


def test_first_run_suggests_people_and_an_uncommented_line_takes_a_role(tmp_path):
    m = tmp_path / "Matter 003"
    m.mkdir()
    (m / "letter.txt").write_text(f"I spoke with {OTHER} yesterday about the appeal.", encoding="utf-8")
    result = run(m)
    assert result.names_created and result.names_suggested == 1
    assert result.summary["names_txt"] == {"entries": 0, "suggestions_added": 1}
    names_txt = m / "_redact" / "names.txt"
    body = names_txt.read_text(encoding="utf-8")
    assert f"# {OTHER} =>" in body
    review = (m / "_redact" / "review.html").read_text(encoding="utf-8")
    assert "listing 1 person the scan found, commented out" in review

    # names.txt is no longer the blank template, so a plain rerun leaves it alone.
    assert run(m).names_suggested == 0
    assert names_txt.read_text(encoding="utf-8") == body

    # Uncomment the suggestion and give it a role; the note after it is a comment.
    names_txt.write_text(body.replace(f"# {OTHER} =>", f"{OTHER} => WITNESS"), encoding="utf-8")
    run(m)
    out = next(iter(outputs(m).values()))
    assert "[WITNESS]" in out and "Fakename" not in out and "Robert" not in out


def test_untouched_template_from_an_older_version_gets_suggestions(tmp_path):
    m = tmp_path / "Matter 004"
    names.write_template(m / "_redact" / "names.txt")
    (m / "letter.txt").write_text(f"I spoke with {OTHER} yesterday about the appeal.", encoding="utf-8")
    result = run(m)
    assert not result.names_created and result.names_suggested == 1


def test_keep_marker_means_not_a_person(tmp_path):
    nf = _parse(tmp_path, "Dexron VI => keep          # [PERSON-3], 2 mentions in 1 file\nJane Example => CLIENT\n")
    assert nf.keep == ["Dexron VI"]
    assert [e.canonical for e in nf.entries] == ["Jane Example"]


def test_keep_marked_suggestion_is_left_in_and_not_suggested_again(tmp_path):
    m = tmp_path / "Matter 006"
    m.mkdir()
    (m / "letter.txt").write_text(f"I spoke with {OTHER} yesterday about the appeal.", encoding="utf-8")
    run(m)
    assert OTHER not in next(iter(outputs(m).values()))
    names_txt = m / "_redact" / "names.txt"
    names_txt.write_text(names_txt.read_text(encoding="utf-8").replace(f"# {OTHER} =>", f"{OTHER} => KEEP"),
                         encoding="utf-8")
    result = run(m, suggest_names=True)
    assert result.names_suggested == 0
    assert OTHER in next(iter(outputs(m).values()))


def test_keep_term_left_over_after_a_names_match_is_not_redacted(tmp_path):
    # The model sometimes sweeps a table header into a name: "Quentin Fakerton Qty Description".
    # names.txt takes the name; the leftover is redacted unless it's on the keep list.
    from conftest import CONFIG

    from redact_local.config import load_profile
    from redact_local.detect import P_NAMES, P_PERSON, Analysis, Engine, Span

    nf = _parse(tmp_path, "Quentin Fakerton => CLIENT\n")
    text = "Quentin Fakerton Qty Description"

    def leftover(keep_terms):
        engine = Engine(load_profile("client", CONFIG), keep_terms, set(), nf)
        result = Analysis(text=text)
        engine._filter(text, [Span(0, 16, "NAME", "names.txt (as written)", "NAMES|CLIENT", fixed="CLIENT", priority=P_NAMES),
                              Span(0, len(text), "PERSON", "spaCy NER", "PERSON|x", priority=P_PERSON)], result)
        return [text[s.start:s.end] for s in result.spans], [k.value for k in result.kept]

    spans, kept = leftover([])
    assert spans == ["Quentin Fakerton", "Qty Description"] and kept == []
    spans, kept = leftover(["Qty Description"])
    assert spans == ["Quentin Fakerton"] and kept == ["Qty Description"]


def _suggested_file(tmp_path):
    p = tmp_path / "names.txt"
    names.write_template(p)
    names.append_suggestions(p, [names.Suggestion("Jane Example", "PERSON-1", 3, 2),
                                 names.Suggestion("Dexron VI", "PERSON-2", 2, 1),
                                 names.Suggestion("Pat Staffer", "PERSON-3", 1, 1),
                                 names.Suggestion("Lee Witness", "PERSON-4", 1, 1)], "client")
    return p


def test_set_roles_by_placeholder(tmp_path):
    p = _suggested_file(tmp_path)
    p.write_text(p.read_text(encoding="utf-8") + "Sam Handtyped\n", encoding="utf-8")
    key = {"PERSON-5": ["Sam Handtyped"], "PERSON-6": ["Robin", "Robin Keyonly"]}
    r = names.set_roles(p, [("PERSON-1", "owner"), ("[PERSON-2]", "KEEP"), ("PERSON-3", ""),
                            ("PERSON-5", "shop"), ("PERSON-6", "witness")], key)
    assert (r.roles, r.kept, r.plain, r.added) == (3, 1, 1, 1)
    nf = names.parse(p)
    assert {(e.canonical, e.placeholder_base) for e in nf.entries} == {
        ("Jane Example", "OWNER"), ("Pat Staffer", None), ("Sam Handtyped", "SHOP"), ("Robin Keyonly", "WITNESS")}
    assert nf.keep == ["Dexron VI"]
    body = p.read_text(encoding="utf-8")
    assert "# [PERSON-1], 3 mentions in 2 files" in body      # the note stays on the line
    assert "# Lee Witness =>" in body                        # not asked for: untouched
    # A second call on the same placeholder replaces the role.
    names.set_roles(p, [("PERSON-1", "CLIENT")], key)
    assert ("Jane Example", "CLIENT") in {(e.canonical, e.placeholder_base) for e in names.parse(p).entries}


def test_set_roles_is_all_or_nothing_and_errors_name_placeholders_only(tmp_path):
    p = _suggested_file(tmp_path)
    before = p.read_text(encoding="utf-8")
    with pytest.raises(UsageError, match="PERSON-99"):
        names.set_roles(p, [("PERSON-1", "OWNER"), ("PERSON-99", "SHOP")], {})
    with pytest.raises(UsageError, match="PERSON-1"):
        names.set_roles(p, [("PERSON-1", "PERSON")], {})
    with pytest.raises(UsageError) as exc:
        names.set_roles(p, [("Jane Example", "OWNER")], {})
    assert "Jane" not in str(exc.value)
    assert p.read_text(encoding="utf-8") == before


def test_set_roles_trusts_the_line_that_produces_the_placeholder_over_a_stale_note(tmp_path):
    # The user copied a suggestion line (note and all) and typed another person's name on it.
    p = tmp_path / "names.txt"
    p.write_text("Pat Staffer =>                          # [PERSON-3], 2 mentions in 1 file\n"
                 "Sam Mechanic =>                         # [PERSON-3], 2 mentions in 1 file\n", encoding="utf-8")
    names.set_roles(p, [("PERSON-3", "MECHANIC")], {}, {1: "PERSON-17", 2: "PERSON-3"})
    assert {(e.canonical, e.placeholder_base) for e in names.parse(p).entries} == {
        ("Pat Staffer", None), ("Sam Mechanic", "MECHANIC")}


def test_set_roles_merges_one_persons_lines(tmp_path):
    p = tmp_path / "names.txt"
    p.write_text("Jane =>                                 # [PERSON-2], 9 mentions in 4 files\n"
                 "Jane Example Address =>                 # [PERSON-2], 1 mention in 1 file\n"
                 "Lee Witness\n"
                 "JaneExampel =>                          # [PERSON-2], 1 mention in 1 file\n", encoding="utf-8")
    current = {1: "PERSON-2", 2: "PERSON-22", 3: "PERSON-8", 4: "PERSON-19"}
    r = names.set_roles(p, [("PERSON-2+PERSON-22+PERSON-19", "OWNER"), ("PERSON-8", "WITNESS")], {}, current)
    assert (r.roles, r.merged) == (2, 2)
    nf = names.parse(p)
    owner = next(e for e in nf.entries if e.placeholder_base == "OWNER")
    assert owner.canonical == "Jane Example"                       # plainest full name first, "Address" dropped
    assert {a.text for a in owner.aliases} == {"Jane Example", "Jane", "JaneExampel"}
    assert len(nf.entries) == 2
    assert (tmp_path / "names.txt.bak").is_file()
    # One line claimed by two different assignments: refused, nothing written.
    before = p.read_text(encoding="utf-8")
    with pytest.raises(UsageError, match=r"\+"):
        names.set_roles(p, [("OWNER", "CLIENT"), ("OWNER", "SHOP")], {}, {1: "OWNER"})
    assert p.read_text(encoding="utf-8") == before


def test_suggest_flag_adds_only_people_not_listed_and_keeps_the_users_lines(matter):
    (matter / "letter.txt").write_text(LETTER, encoding="utf-8")
    names_txt = matter / "_redact" / "names.txt"
    before = names_txt.read_text(encoding="utf-8")
    assert run(matter).names_suggested == 0                  # the user's own names.txt: left alone
    assert names_txt.read_text(encoding="utf-8") == before

    assert run(matter, suggest_names=True).names_suggested >= 1
    body = names_txt.read_text(encoding="utf-8")
    assert body.startswith(before)
    assert f"# {OTHER} =>" in body
    assert "# Quentin Fakerton" not in body and "# Marisol Testwell" not in body   # already listed
    assert run(matter, suggest_names=True).names_suggested == 0
