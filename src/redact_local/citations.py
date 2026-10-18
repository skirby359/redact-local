"""Find legal citations so redaction can leave them alone.

The case-cite, RCW, WAC, constitution and court-rule patterns are adapted from
wa-cite-check (https://github.com/skirby359/wa-cite-check). Here we only need
spans, not parsed components.

A case citation has two parts:
  name part    "State v. Example,"          protected only when the profile says so
  number part  "123 Wn.2d 456, 460 (1994)"  always protected
"""

from __future__ import annotations

import re

# ── Reporter alternation (longest forms first so "App. 2d" wins over "2d") ──
REPORTER = (
    r"Wn\.?\s*App\.?\s*2d|Wash\.?\s*App\.?\s*2d"
    r"|Wn\.?\s*App\.?|Wash\.?\s*App\.?"
    r"|Wn\.?\s*2d|Wash\.?\s*2d|Wn\.?|Wash\.?"
    r"|P\.?\s*3d|P\.?\s*2d"
    r"|F\.?\s*Supp\.?\s*3d|F\.?\s*Supp\.?\s*2d|F\.?\s*Supp\.?"
    r"|F\.?\s*4th|F\.?\s*3d|F\.?\s*2d"
    r"|S\.?\s*Ct\.|U\.?\s*S\.?|L\.?\s*Ed\.?\s*2d|L\.?\s*Ed\.?"
)

_CONNECTOR = r"(?:of|the|and|for|ex|rel\.?|&)"
_PARTY = (
    rf"[A-Z][A-Za-z0-9.'&\-]*"
    rf"(?:\s+(?:{_CONNECTOR}|[A-Z][A-Za-z0-9.'&\-]*)){{0,6}}"
)
_NAME = (
    rf"(?:"
    rf"(?:In re(?:\s+the)?|Matter of|Ex parte|Estate of)\s+{_PARTY}"
    rf"|{_PARTY}\s+v\.?\s+{_PARTY}"
    rf")"
)
NAME_BEFORE = re.compile(rf"({_NAME})\s*,?\s*$")
_NAME_WINDOW = 160

CASE_FULL = re.compile(
    rf"(?P<vol>\d+)\s+(?P<rep>{REPORTER})\s+(?P<page>\d+)"
    rf"(?P<tail>(?:,\s*(?:\d+(?:\s*[-–]\s*\d+)?|\d+\s+(?:{REPORTER})\s+\d+))*)"
    # Court parenthetical: "(1994)", "(Wash. Ct. App. 2019)", "(9th Cir. 2003)". Kept short
    # and court-like, so "(testimony of Jane Example, 2019)" isn't swallowed as citation text.
    rf"(?:[ \t]*\((?P<court>[A-Za-z0-9.&' ]{{0,30}}?)[ \t]*(?P<year>(?:18|19|20)\d{{2}})[ \t]*\))?"
)

RCW_PATTERN = re.compile(r"RCW\s+\d+[A-Z]?\.\d+[A-Z]?\.\d+(?:\(\d+\)(?:\([a-z]\))*)?", re.I)
WAC_PATTERN = re.compile(r"WAC\s+\d+-\d+[A-Z]?-\d+(?:\(\d+\)(?:\([a-z]\))*)?", re.I)
CONST_PATTERN = re.compile(r"Const\.\s+art\.\s+[IVXLC]+(?:,?\s*§\s*\d+(?:\([a-z]\))?)?", re.I)
COURT_RULE_PATTERN = re.compile(
    r"\b(?:CR|CrR|CRLJ|CrRLJ|RAP|RALJ|ER|RPC|GR|MAR|SPR|JuCR|IRLJ)\s+\d+(?:\.\d+)?\b"
)
USC_PATTERN = re.compile(
    r"\b\d+\s+U\.?\s?S\.?\s?C\.?(?:\s*§+\s*|\s+)\d+(?:[A-Za-z0-9-]|\.(?=\w))*(?:\([A-Za-z0-9]+\))*"
)
CFR_PATTERN = re.compile(r"\b\d+\s+C\.?\s?F\.?\s?R\.?(?:\s*§+\s*|\s+)\d+(?:\.\d+)*(?:\([A-Za-z0-9]+\))*")

# Court case numbers and bar numbers (protected only when the profile says so).
CASE_NUMBER_PATTERNS = [
    re.compile(r"\b\d{2}-\d-\d{5}-\d(?:\s+[A-Z]{3})?\b"),         # WA superior court
    re.compile(r"\b\d{5,6}-\d(?:-(?:I{1,3}|IV))?\b"),             # WA appellate / supreme
    re.compile(r"\b\d:\d{2}-(?:cv|cr|mj|md|bk)-\d{3,6}(?:-[A-Z]{2,4})*\b", re.I),  # federal
    re.compile(r"\bWSBA\s*(?:No\.?|#|Number)?\s*:?\s*\d{3,6}\b"),
]

_SIGNAL_WORDS = frozenset({
    "see", "also", "cf", "accord", "contra", "but", "citing", "quoting",
    "compare", "eg", "id", "viz", "cited", "following", "rev'd", "aff'd",
})


def _name_start(text: str, cite_start: int) -> int | None:
    """Start offset of a case name directly before a reporter cite, if any."""
    window_start = max(0, cite_start - _NAME_WINDOW)
    m = NAME_BEFORE.search(text[window_start:cite_start])
    if not m:
        return None
    start = window_start + m.start(1)
    # Drop leading signal words ("See State v. X" -> "State v. X").
    while True:
        word = re.match(r"([A-Za-z.']+)\s+", text[start:cite_start])
        if word and word.group(1).strip(".,'").lower() in _SIGNAL_WORDS:
            start += word.end()
        else:
            break
    return start if start < cite_start else None


def citation_spans(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Return (name_spans, number_spans) for every citation in ``text``."""
    names: list[tuple[int, int]] = []
    numbers: list[tuple[int, int]] = []
    for m in CASE_FULL.finditer(text):
        numbers.append((m.start(), m.end()))
        ns = _name_start(text, m.start())
        if ns is not None:
            names.append((ns, m.start()))
    for pat in (RCW_PATTERN, WAC_PATTERN, CONST_PATTERN, COURT_RULE_PATTERN, USC_PATTERN, CFR_PATTERN):
        numbers.extend((m.start(), m.end()) for m in pat.finditer(text))
    return names, numbers


def case_number_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for pat in CASE_NUMBER_PATTERNS for m in pat.finditer(text)]
