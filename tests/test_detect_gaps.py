"""Detection gaps found on a real matter, rebuilt with synthetic values: a lowercase unit
number, a ZIP+4, an OCR-garbled VIN, a licence plate, and the first name in a
"Last, First" email header."""

from __future__ import annotations

from conftest import CONFIG, outputs, run

from redact_local import patterns


def _engine():
    from redact_local import names
    from redact_local.config import load_common_words, load_keep_terms, load_profile
    from redact_local.detect import Engine

    prof = load_profile("client", CONFIG)
    return Engine(prof, load_keep_terms(prof, CONFIG, None), load_common_words(CONFIG), names.NamesFile())


def _spans(text):
    a = _engine().analyze(text)
    return {(text[s.start:s.end], s.category, s.detector) for s in a.spans}


def _detector_hits(detector, text):
    return [(text[a:b], cat) for a, b, cat, _n in detector(text)]


def test_unit_number_in_any_case_and_zip_plus_4(matter):
    (matter / "note.txt").write_text(
        "Mail it to 1234 Fakewood Ave NE apartment B12\nSeattle, WA 98105-4321 by Friday.\n", encoding="utf-8")
    run(matter)
    out = next(iter(outputs(matter).values()))
    assert "B12" not in out and "98105-4321" not in out and "4321" not in out
    assert "Seattle, WA" in out
    assert _detector_hits(patterns.addresses, "Seattle, WA 98105-4321") == [("98105-4321", "ADDRESS")]
    assert not _detector_hits(patterns.addresses, "Part 98105-4321 shipped")     # no state before it


def test_ocr_garbled_vin_next_to_a_label():
    below = "MODEL ENGINE VIN#\nQZXFAK3RBTRO8LW\nQTY DESCRIPTION"
    before = "MODEL ENGINE WFMDZ4AK9RBBOQ ZQY VIN"
    assert _detector_hits(patterns.vins_near_label, below) == [("QZXFAK3RBTRO8LW", "VIN")]
    assert _detector_hits(patterns.vins_near_label, before) == [("WFMDZ4AK9RBBOQ ZQY", "VIN")]
    # Not without a label, and not a short part number on the same line.
    assert not _detector_hits(patterns.vins_near_label, "QZXFAK3RBTRO8LW")
    assert not _detector_hits(patterns.vins_near_label, "VIN check: part QX7Z2B999A")
    assert not _detector_hits(patterns.vins_near_label, "KEVIN QZXFAK3RBTRO8LW")


def test_licence_plates_next_to_a_label():
    assert _detector_hits(patterns.plates, "License plate: FAK3219") == [("FAK3219", "PLATE")]
    assert _detector_hits(patterns.plates, "LICENSE NO.\n FAK 3219\n") == [("FAK 3219", "PLATE")]
    assert _detector_hits(patterns.plates, "Tag #ZQ1-88X") == [("ZQ1-88X", "PLATE")]
    # A driver's licence is not a plate; a header row of labels is not a value.
    assert not _detector_hits(patterns.plates, "Driver's License No: WDLFAKE0X12Q")
    assert not _detector_hits(patterns.plates, "COLOR LICENSE NO. MILEAGE\n2009 BLUE FAK3219")
    assert not _detector_hits(patterns.plates, "Plate: MILEAGE")


def test_plate_is_redacted_with_its_own_placeholder(matter):
    (matter / "ro.txt").write_text("YEAR 2009 MAKE FAKEMOTOR\nLicense plate: FAK3219\n", encoding="utf-8")
    run(matter)
    out = next(iter(outputs(matter).values()))
    assert "FAK3219" not in out and "[PLATE-1]" in out


def test_last_first_display_name_in_an_email_header():
    text = 'From: "Fakerton, Zelphine (Z.)" <zf@example.net>\nSubject: Parts\n\nHello.\n'
    spans = _spans(text)
    assert ("Fakerton, Zelphine (Z.)", "PERSON", "email header name") in spans
    assert list(patterns.header_display_names('To: Zelphine Fakerton <zf@example.net>, "B, A" <a@b.co>')) \
        == [(4, 21, "Zelphine Fakerton"), (41, 45, "B, A")]


def test_business_display_names_are_left_alone():
    for header in ('From: Northside Example Brake <shop@example.com>', 'From: "Example Law Office" <x@example.com>',
                   "From: Notifications <no-reply@example.com>"):
        assert not any(d == "email header name" for _v, _c, d in _spans(header + "\n"))


# ── Possible misses, most likely first ─────────────────────────────────────

def test_possible_miss_levels():
    from redact_local.detect import Span, _miss_level

    text = "From: Zorbleflax Kqw\nThe Quentin Fakerton Wbxx file\nBest,\nYlvarra\nSome Vqzpl heading\n"
    q = text.index("Quentin Fakerton")
    name = Span(q, q + len("Quentin Fakerton"), "NAME", "names.txt", "x", kind="name")
    email = Span(q, q + len("Quentin Fakerton"), "NAME", "names.txt", "x", kind="email")
    at = lambda w: (text.index(w), text.index(w) + len(w))  # noqa: E731
    assert _miss_level(text, *at("Wbxx"), "proper noun not on any list", [name]) == "likely"   # next to a name
    assert _miss_level(text, *at("Wbxx"), "proper noun not on any list", [email]) == "check"   # not a name span
    assert _miss_level(text, *at("Zorbleflax"), "not in the English word list", []) == "likely"  # header line
    assert _miss_level(text, *at("Ylvarra"), "not in the English word list", []) == "likely"     # after a closing
    assert _miss_level(text, *at("Vqzpl"), "not in the English word list", []) == "low"
    assert _miss_level(text, *at("Vqzpl"), "proper noun not on any list", []) == "check"
    # In OCR'd text the model calls most noise a proper noun: one word drops to low, a run doesn't.
    from redact_local.detect import ocr_ranges

    scan = "--- page 1 ---\n[OCR]\nPart Brshnlp ok\nZelphine Qorvath signed\n"
    ocr = ocr_ranges(scan)
    one = (scan.index("Brshnlp"), scan.index("Brshnlp") + 7)
    two = (scan.index("Zelphine"), scan.index("Qorvath") + 7)
    assert _miss_level(scan, *one, "proper noun not on any list", [], ocr) == "low"
    assert _miss_level(scan, *two, "proper noun not on any list", [], ocr) == "check"


def test_summary_and_review_list_misses_by_level(matter):
    import json

    from conftest import LETTER, out_dir

    (matter / "letter.txt").write_text(LETTER + "\nBest,\nYlvarra Qzm\n", encoding="utf-8")
    run(matter)
    s = json.loads((out_dir(matter) / "_summary.json").read_text(encoding="utf-8"))
    levels = s["possible_misses_by_level"]
    assert set(levels) == {"likely", "check", "low"} and sum(levels.values()) == s["possible_misses"]
    review = (matter / "_redact" / "review.html").read_text(encoding="utf-8")
    section = review.split("<h2>Possible misses", 1)[1].split("<h2>", 1)[0]
    assert section.index("<h3>Likely") < section.index("<h3>Worth a look") < section.index("Probably ordinary words")
