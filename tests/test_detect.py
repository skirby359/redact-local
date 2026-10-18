"""Detectors: structured identifiers, keep list, citations, possible misses."""

from __future__ import annotations

from conftest import ADDRESS, CARD, DOB, LETTER, OTHER, PHONE, SSN, STAFF_EMAIL, TICKET, outputs, run

from redact_local import citations, patterns


def _letter_output(matter, profile="client", text=LETTER):
    (matter / "letter.txt").write_text(text, encoding="utf-8")
    result = run(matter, profile)
    return next(iter(outputs(matter).values())), result


def test_structured_identifiers_are_redacted(matter):
    out, result = _letter_output(matter)
    for value in (PHONE, SSN, CARD, ADDRESS, DOB, TICKET, STAFF_EMAIL, OTHER):
        assert value not in out, value
    for ph in ("[PHONE-1]", "[SSN-1]", "[CARD-1]", "[ADDRESS-1]", "[DOB-1]", "[TICKET-1]", "[EMAIL-1]", "[PERSON-1]"):
        assert ph in out, ph
    # City and state stay.
    assert "Seattle, WA" in out
    assert result.summary["network_attempts_blocked"] == 0


def test_profile_urls_and_handles():
    text = "See github.com/jexample/repo, https://x.com/@jexample2, github.com/settings and @jexample3."
    found = {text[a:b] for a, b, _c, _d in patterns.handles(text)}
    assert {"jexample", "jexample2", "@jexample3"} <= found
    assert "settings" not in found


def test_address_dob_ticket_patterns():
    text = ("Mail: 500 4th Ave S, Suite 210. Or PO Box 1234. Heard in 516 Superior Court. "
            "Date of birth: January 2, 1990. Case No. 23-2-01234-1 SEA. Order 2024 shipped. Ref: AB12345.")
    addrs = {text[a:b] for a, b, *_ in patterns.addresses(text)}
    assert "500 4th Ave S, Suite 210" in addrs and "PO Box 1234" in addrs
    assert not any("Superior Court" in a for a in addrs)
    dobs = {text[a:b] for a, b, *_ in patterns.dates_of_birth(text)}
    assert dobs == {"January 2, 1990"}
    tickets = {text[a:b] for a, b, *_ in patterns.tickets(text)}
    assert "23-2-01234-1" in tickets and "AB12345" in tickets
    assert "2024" not in tickets


def test_keep_list_protects(matter):
    text = ("Trust & Safety, Microsoft and the Northwest Justice Project (NJP) reviewed it. "
            "Two days ago they wrote to us.")
    out, _ = _letter_output(matter, text=text)
    for kept in ("Trust & Safety", "Microsoft", "Northwest Justice Project", "NJP"):
        assert kept in out
    assert "ago" in out and " us." in out


def test_matter_keep_list_overrides_a_detection(matter):
    text = f"I spoke with {OTHER} yesterday about the appeal."
    out, _ = _letter_output(matter, text=text)
    assert OTHER not in out
    (matter / "_redact" / "keep.txt").write_text(f"{OTHER}\n", encoding="utf-8")
    run(matter)
    out = next(iter(outputs(matter).values()))
    assert OTHER in out


def test_all_caps_keep_terms_are_case_sensitive(matter):
    out, result = _letter_output(matter, text="Call us at the office two days ago. The U.S. DOC agreed.")
    # "us" and "ago" are not keep-list hits for "U.S." and "AGO".
    assert result.summary["kept"]["keep_list_occurrences"] == 2  # U.S., DOC


def test_citation_spans_unit():
    text = "See State v. Fakerton, 123 Wn.2d 456, 460 (1994); RCW 9A.04.110(1)(a); WAC 388-14A-1000; 42 U.S.C. § 1983."
    names_, numbers = citations.citation_spans(text)
    got = {text[a:b] for a, b in numbers}
    assert "123 Wn.2d 456, 460 (1994)" in got
    assert "RCW 9A.04.110(1)(a)" in got and "WAC 388-14A-1000" in got and "42 U.S.C. § 1983" in got
    assert {text[a:b] for a, b in names_} == {"State v. Fakerton, "}


def test_citations_survive_in_court_records(matter):
    text = ("Quentin Fakerton filed. See State v. Fakerton, 123 Wn.2d 456, 460 (1994) and RCW 9A.04.110. "
            "Judge Ambrose Fictitious presided. Harriet Q. Nonesuch, WSBA No. 99999, Attorney for Plaintiff. "
            "Zorbleflax was cc'd.\nCall 206-555-0199.")
    out, _ = _letter_output(matter, "court-records", text)
    assert "State v. Fakerton, 123 Wn.2d 456, 460 (1994)" in out
    assert "RCW 9A.04.110" in out
    assert out.count("[CLIENT]") == 1           # the prose mention, not the citation
    assert "Ambrose Fictitious" in out           # titled official
    assert "Harriet Q. Nonesuch" in out          # counsel with WSBA number
    assert "Zorbleflax" not in out               # ordinary person still redacted
    assert "555-0199" not in out                 # contact details still removed


def test_client_profile_redacts_names_in_citations_but_keeps_the_cite(matter):
    text = "See State v. Fakerton, 123 Wn.2d 456, 460 (1994) and RCW 9A.04.110. Judge Ambrose Fictitious presided."
    out, _ = _letter_output(matter, "client", text)
    assert "State v. [CLIENT], 123 Wn.2d 456, 460 (1994)" in out
    assert "RCW 9A.04.110" in out
    assert "Ambrose Fictitious" not in out


def test_unknown_capitalized_word_is_redacted_or_listed_as_a_miss(matter):
    text = "Yesterday Xylophrax Quabbentine phoned. The account id is 99817264 and it was Blorptastic."
    out, result = _letter_output(matter, text=text)
    review = (matter / "_redact" / "review.html").read_text(encoding="utf-8")
    misses_table = review.split("<h2>Possible misses", 1)[1].split("<h2>", 1)[0]
    for word in ("Xylophrax", "Quabbentine", "Blorptastic", "99817264"):
        assert word not in out or word in misses_table, word
