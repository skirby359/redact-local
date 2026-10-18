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
_SUFFIX = "(?:" + "|".join(sorted({*_SUFFIXES, *(s.upper() for s in _SUFFIXES)}, key=len, reverse=True)) + r")\b\.?"
_DIR = r"(?:N|S|E|W|NE|NW|SE|SW|North|South|East|West|Northeast|Northwest|Southeast|Southwest|NORTH|SOUTH|EAST|WEST)\.?"
_UNIT = r"(?:,?\s*(?:Apt|Apartment|Suite|Ste|Unit|Rm|Room|Bldg|Building|Fl|Floor|APT|SUITE|STE|UNIT|#)\.?\s*#?\s*[A-Za-z0-9-]{1,6}\b)"
ADDRESS_RE = re.compile(
    rf"(?<![\w.#-])\d{{1,6}}(?:-\d{{1,4}})?[A-Z]?\s+(?:{_DIR}\s+)?"
    rf"(?:[A-Z0-9][A-Za-z0-9.'-]*\s+){{0,4}}?{_SUFFIX}"
    rf"(?:\s+{_DIR}(?![A-Za-z]))?{_UNIT}?"
)
PO_BOX_RE = re.compile(r"(?i)\bP\.?\s?O\.?\s?Box\s+\d+\b")
_COURT_WORDS = {"superior", "district", "supreme", "municipal", "appeals", "bankruptcy", "circuit", "county", "juvenile", "tax"}


def addresses(text: str) -> Iterator[tuple[int, int, str, str]]:
    for m in ADDRESS_RE.finditer(text):
        words = m.group(0).split()
        # "500 Superior Court" is a court, not an address.
        if len(words) >= 2 and words[-1].rstrip(".").lower() in {"court", "ct"} and words[-2].lower() in _COURT_WORDS:
            continue
        yield m.start(), m.end(), "ADDRESS", "regex:street-address"
    for m in PO_BOX_RE.finditer(text):
        yield m.start(), m.end(), "ADDRESS", "regex:po-box"


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


ALL = (handles, addresses, dates_of_birth, tickets, emails, phones, driver_licenses)


# ── Leftover long numbers (for "possible misses", not redacted) ────────────

LONG_NUMBER_RE = re.compile(r"(?<![\w.])\d[\d -]{4,}\d(?![\w.])")


def long_numbers(text: str) -> Iterator[tuple[int, int]]:
    for m in LONG_NUMBER_RE.finditer(text):
        if sum(c.isdigit() for c in m.group(0)) >= 6:
            yield m.start(), m.end()
