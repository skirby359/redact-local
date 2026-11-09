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
- A line starting with ``#`` is a comment, and so is anything after `` #`` on a line.
- ``=> KEEP`` means "not a person" (``Dexron VI => KEEP``): the value joins the
  matter's keep list for the run instead of being redacted.

The first run also lists the people the scan found, commented out
(``# Jane Example =>    # [PERSON-1], 3 mentions in 2 files``), so the user
uncomments the real ones instead of typing the list from scratch.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from redact_local.config import normalize_term
from redact_local.errors import SafeError, UsageError
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

# "Jane Example => CLIENT   # a note": the note is a comment. Needs whitespace before
# the #, so a # inside a value isn't cut.
_INLINE_COMMENT = re.compile(r"\s#.*$")

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


KEEP_ROLE = "KEEP"


@dataclass
class NamesFile:
    entries: list[NameEntry] = field(default_factory=list)
    bad_lines: int = 0
    exists: bool = False
    # "Dexron VI => KEEP": not a person. Added to this run's keep list, so it stays in the output.
    keep: list[str] = field(default_factory=list)


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
        line = _INLINE_COMMENT.sub("", line).strip()
        if not line:
            continue
        role = None
        if "=>" in line:
            line, role_raw = line.rsplit("=>", 1)
            role = _role_label(role_raw, no)
        aliases = [_classify(a) for a in line.split("|") if a.strip()]
        if not aliases:
            nf.bad_lines += 1
            continue
        if role == KEEP_ROLE:
            nf.keep.extend(a.text.strip() for a in aliases)
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


def is_untouched_template(path: Path) -> bool:
    """True if names.txt is still exactly the template (nobody has edited it)."""
    try:
        return path.read_text(encoding="utf-8-sig") == TEMPLATE
    except (OSError, UnicodeDecodeError):
        return False


# ── Suggestions: people the scan found, written in commented out ───────────

SUGGEST_HEADER = "# ── Suggested by redact-local "
_SUGGEST_COLUMN = 40
_SUGGEST_HELP = [
    "# lines is used yet: each one starts with \"# \".",
    "#",
    "#  - A real person: delete the \"# \" at the start of the line. To give them a",
    "#    role, type it after the =>, e.g.   Jane Example => CLIENT",
    "#  - The same person spelled several ways: put every spelling on ONE line,",
    "#    separated by |, e.g.   Jane Example | J. Q. Exampel => CLIENT",
    "#    (Separate lines become separate placeholders. redact-local roles can merge",
    "#    them: PERSON-1+PERSON-3=CLIENT.)",
    "#  - Not a person (a part name, a line of work notes, a business, a place):",
    "#    delete the \"# \" and type KEEP after the =>, e.g.   Dexron VI => KEEP",
    "#    The next run leaves it in the output and stops suggesting it.",
    "#    Or leave it commented out: it stays redacted.",
    "#",
    "# The [PERSON-n] note is the placeholder it had in review.html when suggested.",
    "#",
]
# Instruction lines from earlier versions, still in names.txt files they wrote.
_LEGACY_HELP = [
    "#    Without a role they keep the [PERSON-n] shown.",
    "#  - The same person on two lines: keep one, and put the other spelling after",
    "#    a |, e.g.   Jane Example | J. Q. Example => CLIENT",
    "# The [PERSON-n] note is the placeholder it has in review.html today.",
    "#  - Not a person (a business, a place, an OCR misread): leave it commented",
    "#    out or delete it. To stop it being redacted, add it to _redact\\keep.txt.",
]
# Our own instruction lines, whose example names must not count as "already listed".
_BOILERPLATE = {line.rstrip() for line in [*TEMPLATE.splitlines(), *_SUGGEST_HELP, *_LEGACY_HELP]}


@dataclass
class Suggestion:
    name: str          # the fullest form the scan saw, e.g. "Jane Example"
    placeholder: str   # what it is in review.html today, without brackets
    mentions: int
    files: int


def _person_key(text: str) -> str:
    """Compare names ignoring case, punctuation and "Last, First" order."""
    tokens = _name_tokens(text)
    return " ".join(re.findall(r"[^\W_]+", " ".join(tokens).casefold()))


def known_aliases(path: Path) -> set[str]:
    """Every name on every line of names.txt, active or commented out, as a comparison key."""
    if not path.is_file():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.rstrip() in _BOILERPLATE:
            continue
        line = _INLINE_COMMENT.sub("", normalize_entry(line).strip().lstrip("#").strip())
        if "=>" in line:
            line = line.rsplit("=>", 1)[0]
        out.update(k for k in (_person_key(a) for a in line.split("|")) if k)
    return out


# Words that label a field in a form or an email header. A detection made only of these
# ("Email", "Full Name") is the label the model took for a person.
_FIELD_LABELS = {
    "email", "e-mail", "mail", "phone", "tel", "telephone", "mobile", "cell", "fax", "name", "full",
    "first", "last", "middle", "surname", "address", "street", "city", "state", "zip", "country",
    "subject", "from", "to", "cc", "bcc", "sent", "date", "reply", "forward", "attachment",
    "attachments", "message", "details", "password", "username", "user", "login", "account",
    "company", "title", "customer", "signature",
}
# Where an inner capital starts a real name: McDonald, MacArthur, FitzGerald, and the
# particles (DeShawn, LaToya, DiCaprio, VanHorn).
_NAME_PREFIXES = _PARTICLES | {"mc", "mac", "fitz", "o"}
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _is_ordinary(word: str, ordinary: set[str]) -> bool:
    w = word.lower()
    return w in ordinary or any(w.endswith(s) and w[: -len(s)] in ordinary for s in ("s", "es", "ed", "d", "ing", "er"))


def _looks_like_code(token: str, ordinary: set[str]) -> bool:
    """An identifier made of ordinary words run together: IsChecked, UseLayoutRounding.

    Real names with an inner capital start with a name prefix (McDonald, DeShawn) or
    have a piece that isn't an ordinary word (JaVale), so they don't match.
    """
    pieces = _CAMEL.split(token.strip(".,;:*()"))
    if len(pieces) < 2 or pieces[0].lower() in _NAME_PREFIXES:
        return False
    return all(_is_ordinary(p, ordinary) for p in pieces)


def clean_suggestion(value: str, ordinary: set[str] = frozenset()) -> str:
    """A detected value made safe to put on a names.txt line ("" if it can't be, or if it
    is plainly not a person: a form label, or code). ``ordinary`` is the English word list.

    Only suggestions are filtered. A value dropped here is still redacted in the output.
    """
    v = " ".join(re.sub(r"=>|[|#]", " ", value).split()).lstrip("@＠").strip(" ,;:.")
    if not re.search(r"[^\W\d_]{2}", v) or _EMAIL.match(v) or _HANDLE.match(v):
        return ""
    toks = v.split()
    # A leftover "Jr" or "van" (the rest of the name was matched by names.txt) is not a person.
    if all(t.lower().strip(".,") in _SUFFIXES | _PARTICLES for t in toks):
        return ""
    if all(t.lower().strip(".,:*") in _FIELD_LABELS for t in toks):
        return ""
    if ordinary and any(_looks_like_code(t, ordinary) for t in toks):
        return ""
    return v


def _suggestion_line(s: Suggestion) -> str:
    left = f"# {s.name} =>"
    note = (f"# [{s.placeholder}], {s.mentions} mention{'' if s.mentions == 1 else 's'} "
            f"in {s.files} file{'' if s.files == 1 else 's'}")
    return f"{left.ljust(_SUGGEST_COLUMN)}  {note}"


def append_suggestions(path: Path, suggestions: list[Suggestion], profile_name: str) -> int:
    """Add suggestions not already on any line of names.txt, commented out. Returns how many.

    Appends only: the user's own lines are never rewritten.
    """
    known = known_aliases(path)
    new, seen = [], set()
    for s in suggestions:
        key = _person_key(s.name)
        if key and key not in known and key not in seen:
            seen.add(key)
            new.append(s)
    if not new:
        return 0
    existing = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    today = date.today().isoformat()
    if SUGGEST_HEADER in existing:
        block = ["#", f"# More found on {today} ({profile_name} profile):"]
    else:
        block = [
            SUGGEST_HEADER.ljust(78, "─"),
            f"# People the scan found on {today} ({profile_name} profile). None of these",
            *_SUGGEST_HELP,
        ]
    block += [_suggestion_line(s) for s in new]
    lead = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(lead + "\n".join(block) + "\n")
    return len(new)


# ── Setting roles by placeholder (``redact-local roles``) ──────────────────

_PLACEHOLDER = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*")


@dataclass
class RolesResult:
    roles: int = 0       # lines given a role
    kept: int = 0        # lines marked KEEP (not a person)
    plain: int = 0       # lines made active with no role (keep their [PERSON-n])
    added: int = 0       # people not on any line, added from key.csv
    merged: int = 0      # lines folded into another line (same person, PERSON-1+PERSON-3=...)
    lines: list[int] = field(default_factory=list)


def _role_for(placeholder: str, role: str) -> str | None:
    role = role.strip()
    if not role:
        return None
    if role.upper() == KEEP_ROLE:
        return KEEP_ROLE
    try:
        return _role_label(role, 0)
    except SafeError:
        raise UsageError(
            f"The role for {placeholder} isn't valid: a role must start with a letter and can't be "
            "a built-in placeholder (PERSON, EMAIL, PHONE, HANDLE, ADDRESS...)."
        ) from None


def _with_role(line: str, role: str | None) -> str:
    """The names.txt line, uncommented, with its role set and any note after it kept."""
    body = line.strip().lstrip("#").strip()
    m = _INLINE_COMMENT.search(body)
    note = body[m.start():].strip() if m else ""
    body = body[:m.start()] if m else body
    if "=>" in body:
        body = body.rsplit("=>", 1)[0]
    new = f"{body.strip()} => {role}" if role else f"{body.strip()} =>"
    return f"{new.ljust(_SUGGEST_COLUMN)}  {note}" if note else new


_LABEL_WORDS = {"address", "name", "customer", "phone", "city", "state", "zip", "date"}


def _strip_label_words(alias: str) -> str:
    """Drop form labels the model swept into a name: "Jane Example Address" -> "Jane Example"."""
    toks = alias.split()
    while len(toks) > 1 and toks[-1].strip(":").lower() in _LABEL_WORDS:
        toks.pop()
    while len(toks) > 1 and toks[0].strip(":").lower() in _LABEL_WORDS:
        toks.pop(0)
    return " ".join(toks)


def _line_parts(line: str) -> tuple[list[str], str]:
    """(aliases, trailing note) of a names.txt line, commented or not."""
    body = line.strip().lstrip("#").strip()
    m = _INLINE_COMMENT.search(body)
    note = body[m.start():].strip() if m else ""
    body = body[:m.start()] if m else body
    if "=>" in body:
        body = body.rsplit("=>", 1)[0]
    return [a.strip() for a in body.split("|") if a.strip()], note


def _merged_line(alias_lists: list[list[str]], role: str | None, note: str) -> str:
    """One line for one person: every spelling, the plainest full name first (restore puts it back)."""
    seen, aliases = set(), []
    for lst in alias_lists:
        for a in lst:
            a = _strip_label_words(a)
            if a and a.casefold() not in seen:
                seen.add(a.casefold())
                aliases.append(a)
    names_only = [a for a in aliases if _classify(a).kind == "name"]
    plain = [a for a in names_only if len(a.split()) >= 2 and all(t[:1].isupper() and t[1:].islower() for t in a.split() if t.isalpha())]
    first = min(plain, key=len) if plain else (names_only[0] if names_only else aliases[0])
    aliases.remove(first)
    body = " | ".join([first, *aliases])
    new = f"{body} => {role}" if role else f"{body} =>"
    return f"{new.ljust(_SUGGEST_COLUMN)}  {note}" if note else new


def set_roles(path: Path, assignments: list[tuple[str, str]], key_values: dict[str, list[str]],
              line_placeholders: dict[int, str] | None = None) -> RolesResult:
    """Give names.txt lines roles by placeholder, e.g. [("PERSON-4", "OWNER"), ("PERSON-9", "KEEP")].

    ``PERSON-1+PERSON-3`` names one person under several placeholders: their lines are
    merged into one line (every spelling kept as an alias, form labels like "Address"
    dropped), which then gets the role.

    A placeholder's line is, in order: the active line(s) that produce it now
    (``line_placeholders``: line number -> placeholder, from key.csv); a commented
    suggestion line with that "# [PERSON-4]" note (notes can be stale once lines are
    copied and edited, so active lines are trusted first); any line with a spelling
    key.csv has for it (``key_values``: placeholder -> spellings). Someone in the key but
    on no line is added. All or nothing: if any placeholder can't be found, nothing is
    written. Errors name placeholders only, never a value.
    """
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    ours = [line.rstrip() in _BOILERPLATE or line.startswith(SUGGEST_HEADER) for line in lines]
    line_keys = []
    for line, skip in zip(lines, ours):
        aliases, _note = _line_parts(normalize_entry(line))
        line_keys.append(set() if skip else {k for k in (_person_key(a) for a in aliases) if k})
    known_lines = {ln - 1 for ln in (line_placeholders or {})}

    def lines_for(ph: str) -> tuple[list[int], list[str]]:
        """(line indexes, spellings to add) for one placeholder."""
        if line_placeholders:
            found = sorted(ln - 1 for ln, p in line_placeholders.items() if p == ph)
            if found:
                return found, []
        note = re.compile(rf"#\s*\[{re.escape(ph)}\](?![\w-])")
        noted = [i for i, line in enumerate(lines) if not ours[i] and "=>" in line and note.search(line)]
        commented = [i for i in noted if lines[i].lstrip().startswith("#")]
        unknown = [i for i in noted if i not in known_lines]
        if commented or unknown:
            return [(commented or unknown)[0]], []
        spellings = [s for s in (" ".join(re.sub(r"=>|[|#]", " ", v).split()) for v in key_values.get(ph, [])) if s]
        keys = {k for k in (_person_key(s) for s in spellings) if k}
        by_key = [i for i, lk in enumerate(line_keys) if lk & keys]
        if by_key:
            return [by_key[0]], []
        if spellings:
            return [], [max(spellings, key=lambda v: (len(v.split()) >= 2, len(v)))]
        return [], []

    plans = []        # (line indexes, extra spellings, role, label for errors)
    missing, claimed = [], {}
    for raw_ph, raw_role in assignments:
        phs = [p.strip().strip("[]").upper() for p in raw_ph.split("+") if p.strip()]
        if not phs or not all(_PLACEHOLDER.fullmatch(p) for p in phs):
            raise UsageError("A placeholder should look like PERSON-4 (with or without the brackets); "
                             "join one person's placeholders with +, e.g. PERSON-1+PERSON-3=OWNER.")
        label = "+".join(phs)
        role = _role_for(label, raw_role)
        idxs, extra = [], []
        for ph in phs:
            i, s = lines_for(ph)
            if not i and not s:
                missing.append(ph)
            idxs += [x for x in i if x not in idxs]
            extra += s
        for i in idxs:
            if i in claimed:
                if claimed[i] == (label, role):
                    continue
                raise UsageError(f"{claimed[i][0]} and {label} share a names.txt line with different roles. "
                                 "Give it one role, and join one person's placeholders with +, "
                                 "e.g. PERSON-1+PERSON-3=OWNER.")
            claimed[i] = (label, role)
        if any(p[:3] == (idxs, extra, role) for p in plans):
            continue                                          # the same assignment twice
        plans.append((idxs, extra, role, label))
    if missing:
        raise UsageError(f"Not in names.txt or the key, so nothing was changed: {', '.join(missing)}.")

    result = RolesResult()
    replace: dict[int, str] = {}
    drop: set[int] = set()
    appended: list[str] = []
    for idxs, extra, role, _label in plans:
        if not idxs:
            appended.append(_merged_line([extra], role, ""))
            result.added += 1
        elif len(idxs) == 1 and not extra:
            replace[idxs[0]] = _with_role(lines[idxs[0]], role)
        else:
            parts = [_line_parts(lines[i]) for i in idxs]
            replace[idxs[0]] = _merged_line([a for a, _n in parts] + [extra], role, parts[0][1])
            drop.update(idxs[1:])
            result.merged += len(idxs) - 1
        if role == KEEP_ROLE:
            result.kept += 1
        elif role:
            result.roles += 1
        else:
            result.plain += 1

    new_lines = []
    for i, line in enumerate(lines):
        if i in drop:
            continue
        new_lines.append(replace.get(i, line))
        if i in replace:
            result.lines.append(len(new_lines))
    for line in appended:
        new_lines.append(line)
        result.lines.append(len(new_lines))

    backup = path.with_suffix(".txt.bak")
    backup.write_text(text, encoding="utf-8")
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    try:
        parse(path)
    except SafeError:
        path.write_text(text, encoding="utf-8")   # put it back as it was
        raise
    return result


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


def _bounded(body: str, glued: bool = False) -> str:
    # Letter-only boundaries: "Fakerton2", "Fakerton_resume", "%20Fakerton" and "@janeexample"
    # all match; "Fakertonville" doesn't. A trailing plural "s" is taken too ("the Fakertons").
    if not glued:
        return rf"(?<![^\W\d_]){body}s?(?![^\W\d_])"
    # Multi-word names also match where OCR or a file name ran them into the next word at a
    # lowercase-to-capital break: "ToJaneExample<", "JaneExampleInvoice". (Case-sensitive
    # checks, so "Donald" alone never matches inside "McDonald": one-word names don't use this.)
    return (rf"(?:(?<![^\W\d_])|(?-i:(?<=[a-z])(?=[A-Z]))){body}s?"
            rf"(?:(?![^\W\d_])|(?-i:(?<=[a-z])(?=[A-Z])))")


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
                    matchers.append(Matcher(re.compile(_bounded(body, glued=word is None), re.I), entry, "name", label))
    return matchers
