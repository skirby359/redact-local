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


def _engine(profile="client"):
    from conftest import CONFIG

    from redact_local import names
    from redact_local.config import load_common_words, load_keep_terms, load_profile
    from redact_local.detect import Engine

    prof = load_profile(profile, CONFIG)
    return Engine(prof, load_keep_terms(prof, CONFIG, None), load_common_words(CONFIG), names.NamesFile())


def _redacted(engine, text, is_name=False):
    return {text[s.start:s.end] for s in engine.analyze(text, is_name=is_name).spans}


def test_person_in_a_possessive_business_name_is_redacted():
    # The model calls "Quentin's Truck Repair" an organization, which the profile keeps.
    engine = _engine()
    assert "Quentin" in _redacted(engine, "Quentin's Truck Repair agreement with Robert Fakename", is_name=True)
    assert "Quentin" in _redacted(engine, "Quentin's truck invoice", is_name=True)
    assert "Quentin" in _redacted(engine, "We signed an agreement with Quentin's Truck Repair for the transmission work.")
    # Ordinary words in possessives are not people.
    for name in ("Driver's License Office", "Children's Hospital records", "Owner's Manual"):
        assert not _redacted(engine, name, is_name=True), name


def test_possessive_names_are_off_in_court_records():
    engine = _engine("court-records")
    assert "Quentin" not in _redacted(engine, "We signed an agreement with Quentin's Truck Repair for the transmission work.")


def test_possessive_business_name_in_a_file_name_is_redacted_in_the_output_name(tmp_path):
    m = tmp_path / "Matter 008"
    (m / "Quentin's truck invoice").mkdir(parents=True)
    (m / "Quentin's truck invoice" / "Quentin's Truck Repair agreement.txt").write_text(
        "Labor: transmission rebuild, 6 hrs.", encoding="utf-8")
    run(m)
    names_out = list(outputs(m))
    assert len(names_out) == 1 and "Quentin" not in names_out[0] and "[PERSON-" in names_out[0]


def test_lowercase_street_type_after_a_street_name_is_an_address():
    text = "Address: 1234 N Fakewood lane, Fakeville. Turn at the 4 way stop. 12 main st."
    found = {text[a:b] for a, b, *_ in patterns.addresses(text)}
    assert "1234 N Fakewood lane" in found
    assert not any("way" in f or "main" in f for f in found)


def test_vin_is_redacted(matter):
    assert patterns.vin_check_digit_ok("1M8GDM9AXKP042788")
    assert not patterns.vin_check_digit_ok("1M8GDM9A1KP042788")
    text = ("Vehicle: 2007 Fakeville truck, VIN 1M8GDM9A1KP042788 (check digit off, but labelled). "
            "Also 1M8GDM9AXKP042788. Not a VIN: 11111111111111111 or print_1785945532620.")
    found = [text[a:b] for a, b, *_ in patterns.vins(text)]
    assert found == ["1M8GDM9A1KP042788", "1M8GDM9AXKP042788"]
    out, _ = _letter_output(matter, text=text)
    assert "[VIN-1]" in out and "[VIN-2]" in out and "1M8GDM9" not in out


def _engine_with_names(tmp_path, body):
    from conftest import CONFIG

    from redact_local import names
    from redact_local.config import load_common_words, load_keep_terms, load_profile
    from redact_local.detect import Engine

    p = tmp_path / "names.txt"
    p.write_text(body, encoding="utf-8")
    prof = load_profile("client", CONFIG)
    return Engine(prof, load_keep_terms(prof, CONFIG, None), load_common_words(CONFIG), names.parse(p))


def test_ocr_glued_misspelled_and_labelled_names(tmp_path):
    engine = _engine_with_names(tmp_path, "Quentin Fakerton => CLIENT\n")
    text = ("--- page 1 ---\n[OCR]\n"
            "ToQuentinFakerton<qf@example.com>\n"
            "NAME Zorbleflax Wibbleton\n"
            "ADDRESS 99 Fakewood Lane\n"
            "Labor on Qventin trans. Parts for Quentn, Fakert on paid. Make sure.\n"
            "--- page 2 ---\n"
            "Typed page: Qventin is a misspelling here too, but this page isn't OCR.\n")
    a = engine.analyze(text)
    redacted = {text[s.start:s.end]: s for s in a.spans}
    for v in ("QuentinFakerton", "Qventin", "Quentn", "Fakert on", "Zorbleflax Wibbleton", "99 Fakewood Lane"):
        assert v in redacted, v
    assert redacted["Qventin"].fixed == "CLIENT"                 # the person's own placeholder
    assert redacted["Zorbleflax Wibbleton"].category == "PERSON"
    assert redacted["99 Fakewood Lane"].category == "ADDRESS"
    page2 = text.index("--- page 2 ---")
    assert not any(s.start > page2 for s in a.spans)            # typed text: no fuzzy matching
    assert "Make" not in redacted                                # an ordinary word is never a misspelling


def test_ocr_name_that_is_also_an_ordinary_word(tmp_path):
    # "Grant" is an English word; OCR context is what makes it a name.
    engine = _engine_with_names(tmp_path, "Grant Fakerton\n")
    text = ("[OCR]\nNAME GrAnt Fakerton\nLabor on grantPed. Grnt paid. Grntstilowed 750\n"
            "We grant the motion.\n")
    spans = engine.analyze(text).spans
    redacted = {text[s.start:s.end] for s in spans}
    for v in ("Grnt", "Grntstilowed"):
        assert v in redacted, v
    assert any("grant" in r for r in redacted)                 # inside "grantPed"
    assert any("GrAnt" in r for r in redacted)
    assert "paid" not in " ".join(redacted)
    we = text.index("We grant")
    assert not any(s.start >= we for s in spans)               # "we grant the motion" stays


def test_image_ocr_is_marked_as_ocr(tmp_path):
    from conftest import render_png

    from redact_local.extract import extract

    ex = extract(render_png(tmp_path / "slip.png", ["NAME Quentin Fakerton"]))
    assert ex.text.startswith("[OCR]\n")


def test_unknown_capitalized_word_is_redacted_or_listed_as_a_miss(matter):
    text = "Yesterday Xylophrax Quabbentine phoned. The account id is 99817264 and it was Blorptastic."
    out, result = _letter_output(matter, text=text)
    review = (matter / "_redact" / "review.html").read_text(encoding="utf-8")
    misses_table = review.split("<h2>Possible misses", 1)[1].split("<h2>", 1)[0]
    for word in ("Xylophrax", "Quabbentine", "Blorptastic", "99817264"):
        assert word not in out or word in misses_table, word
