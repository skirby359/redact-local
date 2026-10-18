"""names.txt: parsing, roles, variants, case-insensitive matching."""

from __future__ import annotations

from conftest import CLIENT_EMAIL, LETTER, outputs, run

from redact_local import names


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
    assert (m / "_redact" / "names.txt").read_text(encoding="utf-8").startswith("# names.txt")
