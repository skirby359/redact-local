"""Regex detectors for things Presidio doesn't cover (or covers loosely).

Each detector yields (start, end, category, detector_name). Where only part of
a match is the identifier (the username in github.com/<user>, the date after
"DOB:"), the span covers that part only, so the context stays readable.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

# ── @handles and profile URLs ──────────────────────────────────────────────

HANDLE_RE = re.compile(
    r"(?<![\w.@＠/])[@＠](?P<h>[A-Za-z0-9](?:[A-Za-z0-9_]|[-.](?=[A-Za-z0-9])){0,38})(?![\w@＠])"
)

# host (with any fixed path prefix) -> the next path segment is a username
_PROFILE_HOSTS = [
    r"github\.com", r"gist\.github\.com", r"gitlab\.com", r"bitbucket\.org", r"codeberg\.org",
    r"twitter\.com", r"x\.com", r"facebook\.com", r"fb\.com", r"instagram\.com",
    r"threads\.net", r"tiktok\.com", r"youtube\.com", r"medium\.com", r"linkedin\.com/in",
    r"reddit\.com/u(?:ser)?", r"bsky\.app/profile", r"keybase\.io", r"npmjs\.com/~",
    r"hub\.docker\.com/u", r"pypi\.org/user", r"news\.ycombinator\.com/user\?id=",
]
PROFILE_RE = re.compile(
    r"(?i)(?<![\w.-])(?:https?://)?(?:www\.|m\.|mobile\.)?(?:" + "|".join(_PROFILE_HOSTS) + r")"
    r"(?:/|(?<==))@?(?P<u>[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38}[A-Za-z0-9_])?)(?![\w-])"
)
# First path segments that are site pages, not users.
RESERVED_PATHS = {
    "about", "account", "accounts", "api", "apps", "blog", "careers", "collections",
    "community", "company", "contact", "copilot", "customer-stories", "dashboard",
    "developer", "developers", "discussions", "docs", "download", "downloads",
    "education", "enterprise", "enterprises", "events", "explore", "features", "gist",
    "github", "groups", "hashtag", "help", "home", "i", "in", "intent", "issues", "jobs",
    "join", "legal", "login", "logout", "marketplace", "messages", "mobile", "new",
    "newsroom", "nonprofit", "notifications", "open-source", "orgs", "organizations",
    "pages", "partners", "password", "password_reset", "policies", "policy", "pricing",
    "privacy", "premium-support", "press", "pulls", "r", "raw", "readme", "resources",
    "search", "security", "sessions", "settings", "share", "sharer", "shop", "signup",
    "site", "sitemap", "skills", "solutions", "sponsors", "stars", "status", "store",
    "support", "team", "teams", "terms", "topics", "tos", "trending", "u", "user",
    "users", "watch", "watching", "why-github", "codespaces", "en", "en-us",
}


def handles(text: str) -> Iterator[tuple[int, int, str, str]]:
    for m in HANDLE_RE.finditer(text):
        yield m.start(), m.end(), "HANDLE", "regex:handle"
    for m in PROFILE_RE.finditer(text):
        if m.group("u").lower() in RESERVED_PATHS:
            continue
        yield m.start("u"), m.end("u"), "HANDLE", "regex:profile-url"


# ── Street addresses (Presidio has no US street-address recognizer) ───────

_SUFFIXES = [
    "Street", "St", "Avenue", "Ave", "Av", "Boulevard", "Blvd", "Road", "Rd", "Drive", "Dr",
    "Lane", "Ln", "Way", "Court", "Ct", "Place", "Pl", "Terrace", "Ter", "Parkway", "Pkwy",
    "Highway", "Hwy", "Circle", "Cir", "Loop", "Trail", "Trl", "Square", "Sq", "Alley",
    "Plaza", "Plz", "Crescent", "Point", "Pt", "Row", "Walk", "Crossing", "Path", "Pike",
]
_SUFFIX_ALTS = "|".join(sorted(_SUFFIXES, key=len, reverse=True))
_SUFFIX = "(?:" + "|".join(sorted({*_SUFFIXES, *(s.upper() for s in _SUFFIXES)}, key=len, reverse=True)) + r")\b\.?"
# Handwritten and OCR'd forms write "1234 N Example lane": any case, but only after a
# capitalized street name, so "a 4 way stop" isn't an address.
_SUFFIX_ANY_CASE = rf"(?i:{_SUFFIX_ALTS})\b\.?"
_DIR = r"(?:N|S|E|W|NE|NW|SE|SW|North|South|East|West|Northeast|Northwest|Southeast|Southwest|NORTH|SOUTH|EAST|WEST)\.?"
# Any case: people write "apartment 4B" as often as "Apt 4B" (the lowercase form was missed).
_UNIT = r"(?:,?\s*(?:(?i:apt|apartment|suite|ste|unit|rm|room|bldg|building|fl|floor)|#)\.?\s*#?\s*[A-Za-z0-9-]{1,6}\b)"
_STREET_WORD = r"[A-Z0-9][A-Za-z0-9.'-]*\s+"
ADDRESS_RE = re.compile(
    rf"(?<![\w.#-])\d{{1,6}}(?:-\d{{1,4}})?[A-Z]?\s+(?:{_DIR}\s+)?"
    rf"(?:(?:{_STREET_WORD}){{0,4}}?{_SUFFIX}|(?:{_STREET_WORD}){{1,4}}?{_SUFFIX_ANY_CASE})"
    rf"(?:\s+{_DIR}(?![A-Za-z]))?{_UNIT}?"
)
PO_BOX_RE = re.compile(r"(?i)\bP\.?\s?O\.?\s?Box\s+\d+\b")
_COURT_WORDS = {"superior", "district", "supreme", "municipal", "appeals", "bankruptcy", "circuit", "county", "juvenile", "tax"}


# ZIP+4 after a state: "Seattle, WA 98105-4321". Nine digits narrow it to a block or a
# building, so it goes; the city, state and plain five-digit ZIP stay, as the profile says.
_STATE = (r"(?:A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[EDAINSOT]|N[EVHJMYCD]|O[HKR]|PA|RI|S[CD]"
          r"|T[NX]|UT|V[TA]|W[AVIY]|PR|GU|VI|Wash(?:ington)?|WASH(?:INGTON)?)")
ZIP4_RE = re.compile(rf"(?<![A-Za-z]){_STATE}\.?,?[ \t]*(?P<zip>\d{{5}}-\d{{4}})(?![\d-])")


def addresses(text: str) -> Iterator[tuple[int, int, str, str]]:
    for m in ADDRESS_RE.finditer(text):
        words = m.group(0).split()
        # "500 Superior Court" is a court, not an address.
        if len(words) >= 2 and words[-1].rstrip(".").lower() in {"court", "ct"} and words[-2].lower() in _COURT_WORDS:
            continue
        yield m.start(), m.end(), "ADDRESS", "regex:street-address"
    for m in PO_BOX_RE.finditer(text):
        yield m.start(), m.end(), "ADDRESS", "regex:po-box"
    for m in ZIP4_RE.finditer(text):
        yield m.start("zip"), m.end("zip"), "ADDRESS", "regex:zip+4"


# ── Dates of birth ─────────────────────────────────────────────────────────

_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?"
    r"|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
)
_DATE = (
    rf"(?:\d{{1,2}}[/.-]\d{{1,2}}[/.-]\d{{2,4}}|\d{{4}}-\d{{1,2}}-\d{{1,2}}"
    rf"|{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}|\d{{1,2}}\s+{_MONTH},?\s+\d{{4}})"
)
DOB_RE = re.compile(
    rf"(?i)\b(?:d\.?\s?o\.?\s?b\.?|date\s+of\s+birth|birth\s*date|birthday|born(?:\s+on)?)"
    rf"\s*[:\-–]?\s*(?P<date>{_DATE})"
)


def dates_of_birth(text: str) -> Iterator[tuple[int, int, str, str]]:
    for m in DOB_RE.finditer(text):
        yield m.start("date"), m.end("date"), "DOB", "regex:date-of-birth"


# ── Ticket, case and account numbers ───────────────────────────────────────

_TICKET_WORDS = (
    r"(?:ticket|case|request|reference|ref|account|acct|order|claim|incident|confirmation"
    r"|invoice|member|customer|support|report|appeal|submission|tracking|transaction|user)"
)
TICKET_RE = re.compile(
    rf"(?i)\b{_TICKET_WORDS}\s*(?:no\.?|number|num\.?|id|#)?(?:\s+(?:is|was))?\s*[:#]?\s*"
    rf"(?P<id>[A-Z0-9](?:[A-Z0-9-]{{2,}})[A-Z0-9])\b"
)
HASH_NUMBER_RE = re.compile(r"(?<![\w&])#\s?(?P<id>\d{5,})\b")
_YEAR = re.compile(r"^(?:19|20)\d{2}$")


def tickets(text: str) -> Iterator[tuple[int, int, str, str]]:
    for m in TICKET_RE.finditer(text):
        ident = m.group("id")
        digits = sum(c.isdigit() for c in ident)
        if digits >= 5 or (digits >= 4 and not _YEAR.match(ident)):
            yield m.start("id"), m.end("id"), "TICKET", "regex:ticket-number"
    for m in HASH_NUMBER_RE.finditer(text):
        yield m.start("id"), m.end("id"), "TICKET", "regex:hash-number"


# ── Backups for Presidio ───────────────────────────────────────────────────

# US phone numbers, as a backstop to Presidio's phonenumbers-based recognizer.
PHONE_RE = re.compile(
    r"(?<![\w.-])(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?![\w-])"
)
# Washington's current driver licence format.
WA_DL_RE = re.compile(r"\bWDL[A-Z0-9]{9}\b")


# Emails, as a backstop to Presidio's recognizer, which only accepts public
# domains (so "jane@corp.local" or "jane@intranet.internal" would get through).
EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![\w-])")


def emails(text: str) -> Iterator[tuple[int, int, str, str]]:
    for m in EMAIL_RE.finditer(text):
        yield m.start(), m.end(), "EMAIL", "regex:email"


def phones(text: str) -> Iterator[tuple[int, int, str, str]]:
    for m in PHONE_RE.finditer(text):
        yield m.start(), m.end(), "PHONE", "regex:us-phone"


def driver_licenses(text: str) -> Iterator[tuple[int, int, str, str]]:
    for m in WA_DL_RE.finditer(text):
        yield m.start(), m.end(), "DRIVER-LICENSE", "regex:wa-driver-license"


# ── Vehicle identification numbers ─────────────────────────────────────────

# 17 characters, no I, O or Q. Position 9 is a check digit on North American VINs; a
# VIN that fails it is still taken when "VIN" is written just before it.
VIN_RE = re.compile(r"(?<![A-Za-z0-9])(?=[A-HJ-NPR-Z0-9]*\d)(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])[A-HJ-NPR-Z0-9]{17}(?![A-Za-z0-9])")
_VIN_LABEL = re.compile(r"(?i)\bV\.?I\.?N\.?\s*(?:no\.?|number|#)?\s*[:#]?\s*$")
_VIN_VALUES = {**{str(d): d for d in range(10)},
               **dict(zip("ABCDEFGH", range(1, 9))), **dict(zip("JKLMN", range(1, 6))), "P": 7, "R": 9,
               **dict(zip("STUVWXYZ", range(2, 10)))}
_VIN_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)


def vin_check_digit_ok(vin: str) -> bool:
    total = sum(_VIN_VALUES[c] * w for c, w in zip(vin.upper(), _VIN_WEIGHTS))
    expected = total % 11
    return vin[8].upper() == ("X" if expected == 10 else str(expected))


def vins(text: str) -> Iterator[tuple[int, int, str, str]]:
    for m in VIN_RE.finditer(text):
        if vin_check_digit_ok(m.group(0)) or _VIN_LABEL.search(text[max(0, m.start() - 20):m.start()]):
            yield m.start(), m.end(), "VIN", "regex:vin"


# A VIN that OCR garbled (a letter misread, one character dropped, a space inserted) is no
# longer 17 clean characters, so VIN_RE misses it. Next to a "VIN" label it is still a VIN:
# repair orders print the label in a header row with the value under it, or after it.
_VIN_WORD = re.compile(r"(?<![A-Za-z])V\.?I\.?N(?![A-Za-z])\.?(?:\s*(?:no\.?|number|#))?\s*[:#]?", re.I)
_VIN_LIKE = re.compile(r"(?<![A-Za-z0-9])[A-Z0-9]{5,}(?: [A-Z0-9]{1,6})?(?![A-Za-z0-9])")


def _label_window(text: str, a: int, b: int) -> tuple[int, int]:
    """From the start of the label's line to the end of the line after it."""
    start = text.rfind("\n", 0, a) + 1
    end = text.find("\n", b)
    if end < 0:
        return start, len(text)
    nxt = text.find("\n", end + 1)
    return start, len(text) if nxt < 0 else nxt


def vins_near_label(text: str) -> Iterator[tuple[int, int, str, str]]:
    for lab in _VIN_WORD.finditer(text):
        start, end = _label_window(text, lab.start(), lab.end())
        for m in _VIN_LIKE.finditer(text, start, end):
            chars = m.group(0).replace(" ", "")
            if (13 <= len(chars) <= 18 and any(c.isdigit() for c in chars)
                    and sum(c.isalpha() for c in chars) >= 4 and not lab.start() <= m.start() < lab.end()):
                yield m.start(), m.end(), "VIN", "regex:vin-near-label"


# ── Licence plates ─────────────────────────────────────────────────────────

# A plate identifies the registered owner as surely as a VIN. Only taken next to a label:
# on the label's line, or first on the next line when the label ends its line. "License
# No." right after "driver('s)" is a driver's licence, which other detectors handle.
_PLATE_LABEL = re.compile(
    r"(?i)(?<![A-Za-z])(?:license\s*plate(?:\s*(?:no\.?|number|#))?|lic(?:ense)?\.?\s*(?:no\.?|number|#)"
    r"|plate\s*(?:no\.?|number|#)?|tag\s*(?:no\.?|number|#))(?:(?<=[#.])|(?![A-Za-z]))\s*[:#]?"
)
_PLATE_LIKE = re.compile(r"(?<![A-Za-z0-9-])(?=[A-Z0-9 -]*\d)(?=[A-Z0-9 -]*[A-Z])[A-Z0-9]{1,4}[- ]?[A-Z0-9]{2,5}(?![A-Za-z0-9-])")


def plates(text: str) -> Iterator[tuple[int, int, str, str]]:
    for lab in _PLATE_LABEL.finditer(text):
        if re.search(r"(?i)driver'?s?\W{0,3}$", text[max(0, lab.start() - 12):lab.start()]):
            continue
        line_end = text.find("\n", lab.end())
        line_end = len(text) if line_end < 0 else line_end
        m = _PLATE_LIKE.match(text, lab.end() + (len(text[lab.end():line_end]) - len(text[lab.end():line_end].lstrip())))
        if m is None and not text[lab.end():line_end].strip() and line_end < len(text):
            nxt = line_end + 1
            nxt += len(text[nxt:]) - len(text[nxt:].lstrip(" \t"))
            m = _PLATE_LIKE.match(text, nxt)
        chars = m.group(0).replace(" ", "").replace("-", "") if m else ""
        if 5 <= len(chars) <= 8 and any(c.isdigit() for c in chars) and any(c.isalpha() for c in chars):
            yield m.start(), m.end(), "PLATE", "regex:licence-plate"


ALL = (handles, addresses, dates_of_birth, tickets, emails, phones, driver_licenses, vins, vins_near_label, plates)


# ── Display names in email headers ─────────────────────────────────────────

# 'From: "Example, Jane (J.)" <jane@x.com>': the model often takes only one half of a
# "Last, First" name. The display name is a person unless it reads as a business; the
# detector decides that, since it needs the English word list.
_HEADER_LINE = re.compile(r"(?im)^[ \t>*]*(?:from|to|cc|bcc|reply-to|sender|on behalf of)[ \t]*:[ \t]*(?P<rest>[^\n]+)")
_DISPLAY_NAME = re.compile(
    r'"(?P<q>[^"\n]{2,80})"[ \t]*<[^<>\n]*@[^<>\n]*>|(?P<u>[^"<>,;:\n@]{2,60}?)[ \t]*<[^<>\n]*@[^<>\n]*>'
)


def header_display_names(text: str) -> Iterator[tuple[int, int, str]]:
    for h in _HEADER_LINE.finditer(text):
        for m in _DISPLAY_NAME.finditer(text, h.start("rest"), h.end("rest")):
            group = "q" if m.group("q") else "u"
            a, b = m.span(group)
            while a < b and not text[a].isalnum():
                a += 1
            while b > a and not (text[b - 1].isalnum() or text[b - 1] in ".)"):
                b -= 1
            if b - a >= 2:
                yield a, b, text[a:b]


# ── Form fields on OCR'd paper forms ───────────────────────────────────────

# "NAME  Jane Exampel" on a sales slip: the model can't read a misspelled name, but the
# label says what it is. Taken when the label has a colon, or is in capitals inside
# OCR'd text (forms print labels in capitals; OCR drops the colon).
FORM_LABEL_RE = re.compile(
    r"(?m)^[ \t]*(?P<label>(?i:customer[ \t]*name|customer|name|street[ \t]*address|address|street))"
    r"(?P<sep>[ \t]*:|[ \t]+)[ \t]*(?P<rest>[^\n]*[^\s])"
)


def form_fields(text: str, ocr: list[tuple[int, int]]) -> Iterator[tuple[int, int, str, str]]:
    for m in FORM_LABEL_RE.finditer(text):
        label = m.group("label")
        in_ocr = any(a <= m.start() < b for a, b in ocr)
        if ":" not in m.group("sep") and not (label.isupper() and in_ocr):
            continue
        rest = m.group("rest")
        if not re.search(r"[^\W\d_]{2}", rest) or re.fullmatch(r"\[[^\]]*\]", rest.strip()):
            continue
        low = label.lower()
        category = "ADDRESS" if ("address" in low or low.startswith("street")) else "PERSON"
        yield m.start("rest"), m.end("rest"), category, f"form label ({' '.join(label.split())})"


# ── Leftover long numbers (for "possible misses", not redacted) ────────────

LONG_NUMBER_RE = re.compile(r"(?<![\w.])\d[\d -]{4,}\d(?![\w.])")


def long_numbers(text: str) -> Iterator[tuple[int, int]]:
    for m in LONG_NUMBER_RE.finditer(text):
        if sum(c.isdigit() for c in m.group(0)) >= 6:
            yield m.start(), m.end()
