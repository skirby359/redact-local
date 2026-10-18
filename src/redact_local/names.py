"""names.txt: the people you want removed, written by you.

Format, one person per line:

    Jane Example | Janie | @jexample | jane@example.com => CLIENT
    Pat Staffer => STAFF
    Sam Otherperson

- Aliases are separated by ``|``. The first one is the name ``restore`` puts back.
- ``=> ROLE`` is optional. With a role, the person becomes ``[ROLE]``; their
  emails, handles and phone numbers become ``[ROLE-EMAIL]``, ``[ROLE-HANDLE]``
  and ``[ROLE-PHONE]``. If two lines share a role they are numbered:
  ``[STAFF-1]``, ``[STAFF-2]``.
- Without a role, the person gets the next ``[PERSON-n]``.
- Matching ignores case. Name variants (``Example, Jane``, ``J. Example``,
  ``Jane`` on its own...) are generated as the profile's ``name_variants`` says.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from redact_local.config import normalize_term
from redact_local.errors import SafeError
from redact_local.textnorm import normalize_entry

TEMPLATE = """\
# names.txt: people to remove from this matter's documents.
# This file stays on your machine. Claude must never open it.
#
# One person per line. Separate aliases with |. Add a role after => if you like.
#
#   Jane Example | Janie | @jexample | jane@example.com => CLIENT
#   Pat Staffer => STAFF
#   Sam Otherperson
#
# With a role, the person shows up as [CLIENT] and their email as [CLIENT-EMAIL].
# Without one, they get the next [PERSON-n].
# Matching ignores case. "Example, Jane", "J. Example", "Jane" and "Example"
# are matched automatically (see name_variants in config/profiles.toml).
"""

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_HANDLE = re.compile(r"^[@＠][A-Za-z0-9][\w.-]*$")
_PHONE = re.compile(r"^[+()\d\s.-]{7,}$")
_PROFILE_URL = re.compile(r"^(?:https?://)?(?:www\.)?[\w.-]+\.\w+/(?:in/|user/|u/|@)?([A-Za-z0-9][\w.-]*)/?$")
_PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "da", "di", "du", "la", "le", "st", "st.", "bin", "ibn", "al", "el"}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "esq", "md", "phd", "dds", "cpa"}
_SUFFIX_TAIL = re.compile(r"(?:,?\s+(?:jr|sr|ii|iii|iv|esq|md|ph\.?d|dds|cpa)\.?)+\s*$", re.I)


def _role_label(raw: str, line_no: int) -> str | None:
    from redact_local.placeholders import PREFIX

    label = re.sub(r"[^A-Z0-9]+", "-", raw.strip().strip("[]").upper()).strip("-")
    if not label:
        return None
    # The message names the line, never the role text: a mistyped role could be a name.
    if not label[0].isalpha():
        raise SafeError(f"names.txt line {line_no}: a role must start with a letter.")
    reserved = "|".join(re.escape(p) for p in set(PREFIX.values()))
    if re.fullmatch(rf"(?:{reserved})(?:-\d+)?", label) or re.search(r"-(?:EMAIL|HANDLE|PHONE)(?:-\d+)?$", label):
        raise SafeError(
            f"names.txt line {line_no}: that role clashes with a built-in placeholder "
            "(PERSON, EMAIL, PHONE, HANDLE, ADDRESS...). Pick another role name."
        )
    return label


@dataclass
class Alias:
    text: str
    kind: str  # name | email | handle | phone


@dataclass
class NameEntry:
    line_no: int
    aliases: list[Alias]
    role: str | None = None
    placeholder_base: str | None = None  # "[CLIENT]" stem without brackets, after numbering

    @property
    def canonical(self) -> str:
        names = [a.text for a in self.aliases if a.kind == "name"]
        return names[0] if names else self.aliases[0].text


@dataclass
class NamesFile:
    entries: list[NameEntry] = field(default_factory=list)
    bad_lines: int = 0
    exists: bool = False


def _classify(alias: str) -> Alias:
    a = alias.strip()
    if _EMAIL.match(a):
        return Alias(a, "email")
    if _HANDLE.match(a):
        return Alias(a, "handle")
    m = _PROFILE_URL.match(a)
    if m and "/" in a:
        return Alias("@" + m.group(1), "handle")
    if _PHONE.match(a) and sum(c.isdigit() for c in a) >= 7:
        return Alias(a, "phone")
    return Alias(a, "name")


def parse(path: Path, existing: dict[str, str] | None = None) -> NamesFile:
    """Read names.txt. ``existing`` maps placeholders already in key.csv to their values."""
    nf = NamesFile(exists=path.is_file())
    if not nf.exists:
        return nf
    for no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = normalize_entry(line).strip()
        if not line or line.startswith("#"):
            continue
        role = None
        if "=>" in line:
            line, role_raw = line.rsplit("=>", 1)
            role = _role_label(role_raw, no)
        aliases = [_classify(a) for a in line.split("|") if a.strip()]
        if not aliases:
            nf.bad_lines += 1
            continue
        nf.entries.append(NameEntry(no, aliases, role))
    assign_placeholders(nf, existing or {})
    return nf


def assign_placeholders(nf: NamesFile, existing: dict[str, str]) -> None:
    """Give each role entry its placeholder, keeping the ones key.csv already has.

    A role used once is ``[ROLE]``; a shared role is numbered. Numbers stick to
    the person (matched by their first alias), so adding someone to names.txt
    never renumbers the people already there, and old drafts still restore.
    """
    by_role: dict[str, list[NameEntry]] = defaultdict(list)
    for e in nf.entries:
        if e.role:
            by_role[e.role].append(e)
    for role, entries in by_role.items():
        pattern = re.compile(rf"{re.escape(role)}(?:-\d+)?")
        taken = {ph: v for ph, v in existing.items() if pattern.fullmatch(ph)}
        claimed: set[str] = set()
        fresh: list[NameEntry] = []
        for e in entries:
            key = normalize_term(e.canonical)
            match = next((ph for ph, v in sorted(taken.items()) if ph not in claimed and normalize_term(v) == key), None)
            if match:
                e.placeholder_base = match
                claimed.add(match)
            else:
                fresh.append(e)
        used = set(taken) | claimed
        if len(entries) == 1 and fresh and role not in used:
            fresh[0].placeholder_base = role
            continue
        n = 2 if role in used else 1
        for e in fresh:
            while f"{role}-{n}" in used:
                n += 1
            e.placeholder_base = f"{role}-{n}"
            used.add(e.placeholder_base)


def write_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")


# ── Matching ───────────────────────────────────────────────────────────────

@dataclass
class Matcher:
    regex: re.Pattern[str]
    entry: NameEntry
    kind: str
    variant: str
    alias: str = ""
    index: int = 1  # 1-based position among this entry's aliases of the same kind


_SEP = r"[\s_.\-]*"  # "Jane Example", "jane_example", "jane.example", "JaneExample"


def _name_tokens(name: str) -> list[str]:
    """Tokens in first-to-last order, without suffixes ("Example, Jane Q., Jr." -> Jane Q. Example)."""
    name = _SUFFIX_TAIL.sub("", name.strip())
    if name.count(",") == 1:
        last, first = (part.strip() for part in name.split(","))
        if last and first:
            name = f"{first} {last}"
    tokens = [t for t in re.split(r"[\s_,]+", name) if t]
    while len(tokens) > 1 and tokens[-1].lower().strip(".") in _SUFFIXES:
        tokens.pop()
    return tokens


def _split_first_last(tokens: list[str]) -> tuple[str, list[str], list[str]]:
    """(first, middles, last tokens) with particles kept on the surname."""
    first = tokens[0]
    rest = tokens[1:]
    if not rest:
        return first, [], []
    i = len(rest) - 1
    while i > 0 and rest[i - 1].lower().strip(".") in _PARTICLES:
        i -= 1
    return first, rest[:i], rest[i:]


def _seq_regex(tokens: list[str]) -> str:
    parts = []
    for t in tokens:
        for piece in (p for p in t.split("-") if p):  # "Testwell-Smith" also matches "TESTWELL SMITH"
            bare = piece.rstrip(".")
            parts.append(re.escape(bare) + (r"\.?" if len(bare) == 1 or piece.endswith(".") else ""))
    return _SEP.join(parts)


def _bounded(body: str) -> str:
    # Letter-only boundaries: "Fakerton2", "Fakerton_resume", "%20Fakerton" and "@janeexample"
    # all match; "Fakertonville" doesn't. A trailing plural "s" is taken too ("the Fakertons").
    return rf"(?<![^\W\d_]){body}s?(?![^\W\d_])"


def _name_variants(name: str, wanted: list[str]) -> list[tuple[str, str, str | None]]:
    """(variant label, regex body, the word if it is a one-word variant) for one name alias."""
    tokens = _name_tokens(name)
    if not tokens:
        return []
    out: list[tuple[str, str, str | None]] = []
    first, middles, last = _split_first_last(tokens)
    # The alias as written is always matched (suffixes and "Last, First" order included).
    written = [t for t in re.split(r"[\s_]+", name.strip()) if t]
    out.append(("as written", _seq_regex(written), written[0] if len(written) == 1 else None))
    if len(tokens) == 1 or not last:
        return out
    if tokens != written:
        out.append(("full", _seq_regex(tokens), None))
    if "full" in wanted and middles:
        out.append(("full", _seq_regex([first, *last]), None))
    if "last-first" in wanted:
        out.append(("last-first", _seq_regex(last) + r"\s*,\s*" + _seq_regex([first, *middles]), None))
        if middles:
            out.append(("last-first", _seq_regex(last) + r"\s*,\s*" + _seq_regex([first]), None))
    if "initial-last" in wanted:
        out.append(("initial-last", re.escape(first[0]) + r"\.?\s*" + _seq_regex(last), None))
    if "first" in wanted and len(first.rstrip(".")) > 1:
        out.append(("first", _seq_regex([first]), first))
    if "last" in wanted:
        single = last[0] if len(last) == 1 and "-" not in last[0] else None
        out.append(("last", _seq_regex(last), single))
        # Each half of a hyphenated surname on its own ("Testwell" from "Testwell-Smith").
        if len(last) == 1 and "-" in last[0]:
            for piece in (p for p in last[0].split("-") if len(p) >= 3):
                out.append(("last", _seq_regex([piece]), piece))
    return out


def _phone_regex(text: str) -> str:
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    body = r"[\s().-]*".join(re.escape(d) for d in digits)
    return rf"(?<!\d)(?:\+?1[\s.-]*)?\(?{body}(?!\d)"


def build_matchers(nf: NamesFile, variants: list[str], on_keep_list, ordinary: set[str]) -> list[Matcher]:
    """Compile regexes for every alias and variant in names.txt.

    ``on_keep_list(word)`` says whether a word is a keep-list term: a one-word
    variant on the keep list is skipped (so a client named "Jane Washington" doesn't take "Washington"
    with her). ``ordinary`` is the English word list: a one-word variant that is
    an ordinary word only matches when capitalized.
    """
    matchers: list[Matcher] = []
    for entry in nf.entries:
        seen: Counter = Counter()
        for alias in entry.aliases:
            seen[alias.kind] += 1
            idx = seen[alias.kind]
            if alias.kind == "email":
                rx = re.compile(rf"(?<![\w.+-]){re.escape(alias.text)}(?![\w-]|\.\w)", re.I)
                matchers.append(Matcher(rx, entry, "email", "as written", alias.text, idx))
            elif alias.kind == "handle":
                bare = alias.text.lstrip("@＠")
                # Not inside a longer alphanumeric run, but "qfakerton_old" and "x.qfakerton"
                # count. Not the local part of an email: the email detector takes that whole.
                rx = re.compile(rf"(?<![A-Za-z0-9])[@＠]?{re.escape(bare)}(?![A-Za-z0-9])(?![@＠])", re.I)
                matchers.append(Matcher(rx, entry, "handle", "as written", alias.text, idx))
            elif alias.kind == "phone":
                matchers.append(Matcher(re.compile(_phone_regex(alias.text)), entry, "phone", "as written", alias.text, idx))
            else:
                for label, body, word in _name_variants(alias.text, variants):
                    if word is not None:
                        bare = word.rstrip(".")
                        if label != "as written" and (on_keep_list(bare) or len(bare) < 2):
                            continue
                        if bare.lower() in ordinary:
                            # Capitalized or ALL CAPS only: "Grant" the person, not "grant" the verb.
                            forms = {bare, bare[:1].upper() + bare[1:], bare[:1].upper() + bare[1:].lower(), bare.upper()}
                            alt = "|".join(re.escape(f) for f in sorted(forms))
                            matchers.append(Matcher(re.compile(_bounded(f"(?:{alt})")), entry, "name", label))
                            continue
                    matchers.append(Matcher(re.compile(_bounded(body), re.I), entry, "name", label))
    return matchers
