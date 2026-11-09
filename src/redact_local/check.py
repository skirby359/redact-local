"""Look for personal data a run left in the output, before anyone reads it.

    redact-local check <matter>

Reads the matter's redacted output, ``key.csv`` and ``names.txt`` and looks for:

1. Known values: a spelling the key holds, or an alias on any names.txt line (commented-out
   suggestions too), still in the output. It was redacted somewhere, so finding it elsewhere
   is a miss.
2. Name words: one word of a person's known spelling, capitalized, on its own.
3. Neighbours: a capitalized word right next to a person's placeholder ("[PERSON-3], Jane",
   "JANE' [PERSON-3]"): usually the half of the name nobody listed.
4. Shapes: the run's own pattern detectors (addresses, VINs, plates, emails, phones...) run
   again over the output, plus SSNs and card numbers.
5. names.txt problems: one role on several lines (numbered as different people), a role typed
   on a line that is still commented out, two lines carrying the same suggestion note.

Ordinary English words, keep-list terms, form labels and citations are skipped.

The console gets counts, finding ids, placeholders and names.txt line numbers only. Each
finding, with its text, file and line, goes to ``_redact/check.txt`` (for the user, never for
Claude) and ``_redact/check.json`` (for ``adopt``, which adds a finding's word to that
person's names.txt line without anyone typing it).
"""

from __future__ import annotations

import bisect
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from redact_local import build, citations, names, patterns
from redact_local.config import (
    config_dir, flexible_term_regex, load_common_words, load_keep_terms, load_profile, normalize_term,
)
from redact_local.errors import SafeError
from redact_local.pipeline import OUT_NAME, PRIVATE, matter_paths
from redact_local.placeholders import Registry

CHECK_TXT, CHECK_JSON = "check.txt", "check.json"
_PH = re.compile(r"\[([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*)\]")
_CONTACT_SUFFIX = re.compile(r"-(?:EMAIL|PHONE|HANDLE)(?:-\d+)?$")
_NOTE = re.compile(r"#\s*\[([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*)\]")
_WORD = r"[A-Za-z][A-Za-z'’-]*"
# Words that sit next to a name without being part of it.
_BESIDE_NAMES = {
    "dear", "hi", "hello", "hey", "mr", "mrs", "ms", "miss", "mx", "dr", "attn", "re", "fwd", "fw",
    "cc", "bcc", "from", "to", "by", "per", "thanks", "regards", "sincerely", "via", "and", "or",
}
_SSN = re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])")
_CARD = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")
_DIGIT_CATEGORIES = {"PHONE", "SSN", "CARD"}


@dataclass
class Hit:
    file: str
    line: int
    context: str


@dataclass
class Finding:
    id: str
    kind: str                # known | word | neighbour | shape | names
    placeholder: str = ""    # whose it is, if anyone's
    names_line: int = 0      # names.txt line, if known
    category: str = ""
    value: str = ""          # private: check.txt / check.json only
    note: str = ""           # names.txt problems: a description without values
    hits: list[Hit] = field(default_factory=list)

    @property
    def files(self) -> int:
        return len({h.file for h in self.hits})


@dataclass
class CheckResult:
    matter: Path
    files: int
    findings: list[Finding]
    skipped_values: int      # ordinary words, keep-list terms and form labels not searched
    report: Path
    data: Path

    def of(self, kind: str) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]


def current_lines(names_path: Path, registry: Registry) -> dict[int, str]:
    """names.txt line number -> the placeholder that active line produces now."""
    from redact_local.detect import ident_for

    out = {}
    if not names_path.is_file():
        return out
    for e in names.parse(names_path).entries:
        ph = e.placeholder_base or registry.by_ident.get(ident_for("PERSON", e.canonical))
        if ph:
            out[e.line_no] = ph
    return out


def key_spellings(registry: Registry) -> dict[str, list[str]]:
    return {ph: [v for v in [registry.primary.get(ph), *values] if v] for ph, values in registry.rows.items()}


class _Words:
    """Ordinary-word, keep-list and form-label tests, shared by every check."""

    def __init__(self, english: set[str], common: set[str], keep_terms: list[str]):
        self.english, self.common = english, common
        self.keep_norm = {normalize_term(t) for t in keep_terms}
        self.keep_res = [re.compile(flexible_term_regex(t), re.I) for t in keep_terms]

    def ordinary(self, word: str) -> bool:
        w = word.lower().strip("'’-.")
        forms = {w, w[:-1] if w.endswith("s") else w, w[:-2] if w.endswith("es") else w}
        return any(f in self.english or f in self.common for f in forms if f)

    def label(self, word: str) -> bool:
        return word.lower().strip("'’-.:") in names._FIELD_LABELS

    def kept(self, value: str) -> bool:
        return normalize_term(value) in self.keep_norm

    def skip_word(self, word: str) -> bool:
        w = word.strip("'’-.")
        low = w.lower()
        return (len(w) < 2 or self.ordinary(w) or self.label(w) or self.kept(w)
                or low in _BESIDE_NAMES or low in names._PARTICLES or low in names._SUFFIXES)

    def keep_spans(self, text: str) -> list[tuple[int, int]]:
        return [m.span() for rx in self.keep_res for m in rx.finditer(text)]

    def mostly_kept(self, value: str) -> bool:
        """A keep-list term plus ordinary words ("Example Brakes" when "Example" is kept)."""
        rest = value
        for rx in self.keep_res:
            rest = rx.sub(" ", rest)
        if rest == value:
            return False
        return all(self.skip_word(t) for t in re.findall(r"[^\W\d_]+", rest))


class _Doc:
    """One output file: its text, the same text with placeholders and the header blanked,
    and a line-number lookup."""

    def __init__(self, path: Path, rel: str):
        self.rel = rel
        self.text = path.read_text(encoding="utf-8", errors="replace")
        masked = _PH.sub(lambda m: " " * len(m.group(0)), self.text)
        if self.text.startswith("[redact-local "):
            end = self.text.find("\n")
            end = len(self.text) if end < 0 else end
            masked = " " * end + masked[end:]
        self.masked = masked
        self.starts = [0] + [m.end() for m in re.finditer("\n", self.text)]

    def line_of(self, pos: int) -> int:
        return bisect.bisect_right(self.starts, pos)

    def hit(self, a: int, b: int) -> Hit:
        ctx = self.text[max(0, a - 60):b + 60].replace("\r", "").replace("\n", " / ")
        return Hit(self.rel, self.line_of(a), ctx)


def _value_regex(value: str, category: str) -> re.Pattern | None:
    if category in _DIGIT_CATEGORIES:
        digits = re.sub(r"\D", "", value)
        if len(digits) < 7:
            return None
        return re.compile(r"(?<!\d)" + r"[\s().-]{0,3}".join(digits) + r"(?!\d)")
    tokens = re.findall(r"[^\W_]+", value)
    if not tokens or sum(len(t) for t in tokens) < 3:
        return None
    body = r"[\W_]{0,3}".join(re.escape(t) for t in tokens)
    return re.compile(rf"(?<![^\W_]){body}(?![^\W_])", re.I)


def _capitalized(s: str) -> bool:
    first = next((c for c in s if c.isalpha()), "")
    return bool(first) and first.isupper()


def _names_lines(names_path: Path, registry: Registry) -> list[tuple[int, str, bool, list[str], str]]:
    """(line number, placeholder, active, aliases, role or "") for every person line."""
    if not names_path.is_file():
        return []
    current = current_lines(names_path, registry)
    out = []
    for no, raw in enumerate(names_path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if raw.rstrip() in names._BOILERPLATE or raw.startswith(names.SUGGEST_HEADER) or not raw.strip():
            continue
        active = not raw.lstrip().startswith("#")
        aliases, _note = names._line_parts(names.normalize_entry(raw))
        if not aliases or (not active and "=>" not in raw):
            continue              # a plain comment, not a person line
        note = _NOTE.search(raw)
        ph = current.get(no) or (note.group(1) if note else "")
        out.append((no, ph, active, [names._strip_quotes(a) for a in aliases], names._line_role(raw) or ""))
    return out


def run_check(path, config=None) -> CheckResult:
    from redact_local import detect

    matter, _scope = matter_paths(path)
    private, out_dir = matter / PRIVATE, matter / OUT_NAME
    if not out_dir.is_dir() or not (private / "key.csv").is_file():
        raise SafeError("This matter has no redacted output yet. Run it first.")
    profile_name = "client"
    try:
        profile_name = json.loads((out_dir / "_summary.json").read_text(encoding="utf-8")).get("profile") or "client"
    except (OSError, ValueError):
        pass
    cfg = config_dir(config)
    profile = load_profile(profile_name, cfg)
    names_path = private / "names.txt"
    nf = names.parse(names_path) if names_path.is_file() else names.NamesFile()
    words = _Words(detect.english_words(detect._analyzer(profile.spacy_model), profile.spacy_model),
                   load_common_words(cfg), load_keep_terms(profile, cfg, private) + nf.keep)
    registry = Registry(private / "key.csv")
    docs = [_Doc(p, str(p.relative_to(out_dir))) for p in sorted(out_dir.rglob("*.txt"))]
    person_phs = {ph for ph, cat in registry.category.items()
                  if cat in {"PERSON", "NAME"} and not _CONTACT_SUFFIX.search(ph)}
    lines = _names_lines(names_path, registry)
    line_of_ph, line_of_value = {}, {}
    for no, ph, active, aliases, _role in lines:
        if ph and (active or ph not in line_of_ph):
            line_of_ph[ph] = no
        for a in aliases:
            line_of_value.setdefault(" ".join(re.findall(r"[^\W_]+", a.casefold())), no)

    def names_line(ph: str, value: str) -> int:
        return line_of_ph.get(ph) or line_of_value.get(" ".join(re.findall(r"[^\W_]+", value.casefold())), 0)

    findings: list[Finding] = []
    by_key: dict[tuple, Finding] = {}

    def finding(kind: str, key: tuple, **kw) -> Finding:
        f = by_key.get((kind, *key))
        if f is None:
            f = Finding(id="", kind=kind, **kw)
            by_key[(kind, *key)] = f
            findings.append(f)
        return f

    # 1. Known values: key spellings and names.txt aliases.
    values: dict[str, tuple[str, str, str]] = {}      # casefold -> (value, placeholder, category)
    for ph, spellings in key_spellings(registry).items():
        for v in spellings:
            values.setdefault(v.strip().casefold(), (v.strip(), ph, registry.category.get(ph, "")))
    for _no, ph, _active, aliases, role in lines:
        if role.upper() == names.KEEP_ROLE:
            continue
        for a in aliases:
            kind = names._classify(a).kind
            cat = {"email": "EMAIL", "phone": "PHONE", "handle": "HANDLE"}.get(kind, "NAME")
            values.setdefault(a.casefold(), (a, ph, cat))
    skipped = 0
    keep_phs = {ph for _no, ph, active, _a, role in lines if ph and active and role.upper() == names.KEEP_ROLE}
    known_spans: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for value, ph, cat in values.values():
        tokens = re.findall(r"[^\W_]+", value)
        if (ph in keep_phs or words.kept(value) or words.mostly_kept(value)
                or (len(tokens) == 1 and cat not in _DIGIT_CATEGORIES and words.skip_word(tokens[0]))):
            skipped += 1
            continue
        rx = _value_regex(value, cat)
        if rx is None:
            skipped += 1
            continue
        for d in docs:
            for m in rx.finditer(d.masked):
                if cat in {"PERSON", "NAME"} and len(tokens) == 1 and not _capitalized(m.group(0)):
                    continue      # "will" the word, not Will the person
                f = finding("known", (value.casefold(),), placeholder=ph, names_line=names_line(ph, value),
                            category=cat, value=value)
                f.hits.append(d.hit(*m.span()))
                known_spans[d.rel].append(m.span())

    # 2. Name words: one word of a person's spelling, capitalized, on its own.
    person_words: dict[str, set[str]] = defaultdict(set)
    for value, ph, cat in values.values():
        if cat not in {"PERSON", "NAME"} or not ph or ph in keep_phs or words.mostly_kept(value):
            continue
        toks = [names._strip_quotes(t) for t in re.findall(r"[^\W\d_][^\W\d_'’-]*", value)]
        if len(toks) < 2:
            continue
        for t in toks:
            if len(t) >= 3 and not words.skip_word(t) and t.casefold() not in values:
                person_words[t.casefold()].add(ph)
    for word, phs in person_words.items():
        rx = re.compile(rf"(?<![^\W_]){re.escape(word)}(?![^\W_])", re.I)
        ph = sorted(phs)[0]
        for d in docs:
            for m in rx.finditer(d.masked):
                if not _capitalized(m.group(0)) or any(a <= m.start() < b for a, b in known_spans[d.rel]):
                    continue
                f = finding("word", (word,), placeholder=ph, names_line=line_of_ph.get(ph, 0),
                            category="NAME", value=m.group(0))
                f.hits.append(d.hit(*m.span()))

    # 3. Neighbours: a capitalized word right next to a person's placeholder.
    for d in docs:
        t = d.text
        for m in _PH.finditer(t):
            ph = m.group(1)
            if ph not in person_phs:
                continue
            ls = t.rfind("\n", 0, m.start()) + 1
            le = t.find("\n", m.end())
            le = len(t) if le < 0 else le
            before = re.search(rf"({_WORD})[ \t,\"(]{{0,3}}$", t[ls:m.start()])
            after = re.match(rf"[ \t,\"'’(]{{0,3}}({_WORD})", t[m.end():le])
            for grp, a in ((before, ls + before.start(1) if before else 0), (after, m.end() + after.start(1) if after else 0)):
                if not grp:
                    continue
                w = grp.group(1).strip("'’-")
                if not w or not w[0].isupper() or words.skip_word(w) or _PH.fullmatch(f"[{w}]"):
                    continue
                f = finding("neighbour", (ph, w.casefold()), placeholder=ph, names_line=line_of_ph.get(ph, 0),
                            category="NAME", value=w)
                if w.istitle() and not f.value.istitle():
                    f.value = w        # adopt adds the plainest form
                f.hits.append(d.hit(a, a + len(grp.group(1))))

    # 4. Shapes the run's detectors find again, plus SSNs and card numbers.
    for d in docs:
        skip = words.keep_spans(d.masked)
        name_spans, number_spans = citations.citation_spans(d.masked)
        skip += name_spans + number_spans
        if profile.protect_case_numbers:
            skip += citations.case_number_spans(d.masked)
        hits = [(a, b, cat) for det in patterns.ALL for a, b, cat, _n in det(d.masked)]
        hits += [(m.start(), m.end(), "SSN") for m in _SSN.finditer(d.masked)]
        hits += [(m.start(), m.end(), "CARD") for m in _CARD.finditer(d.masked) if _luhn(m.group(0))]
        for a, b, cat in hits:
            if any(s0 <= a and b <= s1 for s0, s1 in skip):
                continue
            value = d.masked[a:b]
            f = finding("shape", (cat, re.sub(r"\s+", " ", value).casefold()), category=cat, value=value)
            f.hits.append(d.hit(a, b))

    # 5. names.txt problems (described by line number only).
    roles = defaultdict(list)
    notes = defaultdict(list)
    for no, ph, active, _aliases, role in lines:
        if active and role and role.upper() != names.KEEP_ROLE:
            roles[role.upper()].append(no)
        if not active and role:
            finding("names", (no, "commented role"), names_line=no,
                    note=f"line {no}: a role is typed, but the line still starts with '#', so it is ignored")
    if names_path.is_file():
        for no, raw in enumerate(names_path.read_text(encoding="utf-8-sig").splitlines(), 1):
            m = _NOTE.search(raw)
            if m and raw.rstrip() not in names._BOILERPLATE:
                notes[m.group(1)].append(no)
    for _role, nos in roles.items():
        if len(nos) > 1:
            finding("names", (tuple(nos), "role"), names_line=nos[0],
                    note=f"lines {', '.join(map(str, nos))}: the same role on {len(nos)} lines, so they become "
                         f"different people (numbered). One person? Merge them: roles <matter> A+B=ROLE")
    for ph, nos in notes.items():
        if len(nos) > 1:
            finding("names", (tuple(nos), "note"), placeholder=ph, names_line=nos[0],
                    note=f"lines {', '.join(map(str, nos))} both came from [{ph}]: probably one person spelled two ways")

    order = {"known": 0, "neighbour": 1, "word": 2, "shape": 3, "names": 4}
    findings.sort(key=lambda f: (order[f.kind], -len(f.hits), f.placeholder, f.category))
    for n, f in enumerate(findings, 1):
        f.id = f"F{n}"
    report, data = private / CHECK_TXT, private / CHECK_JSON
    _write(report, data, matter, docs, findings, skipped)
    return CheckResult(matter, len(docs), findings, skipped, report, data)


def _luhn(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    total = 0
    for i, dgt in enumerate(reversed(digits)):
        if i % 2:
            dgt = dgt * 2 - 9 if dgt > 4 else dgt * 2
        total += dgt
    return len(digits) >= 13 and total % 10 == 0


_KIND_TEXT = {
    "known": "known value still in the output",
    "neighbour": "capitalized word next to a person's placeholder",
    "word": "word of a person's name on its own",
    "shape": "identifier shape",
    "names": "names.txt problem",
}


def _write(report: Path, data: Path, matter: Path, docs, findings: list[Finding], skipped: int) -> None:
    lines = [
        f"redact-local {build()} check, {datetime.now():%Y-%m-%d %H:%M}",
        f"Matter: {matter}",
        "THIS FILE CONTAINS THE ORIGINAL VALUES. It stays on this machine. Don't let Claude open it.",
        f"Output files checked: {len(docs)}. Values skipped as ordinary words, keep-list terms or labels: {skipped}.",
        "",
        "How to act on a finding:",
        "  - A real person, or part of one: add it to names.txt (or: redact-local adopt <matter> <id>).",
        "  - Harmless (a word, a product, a business): '=> KEEP' on its names.txt line, or keep.txt.",
        "",
    ]
    for f in findings:
        head = f"{f.id}  {_KIND_TEXT[f.kind]}"
        if f.placeholder:
            head += f"  [{f.placeholder}]"
        if f.names_line:
            head += f"  names.txt line {f.names_line}"
        if f.category and f.kind == "shape":
            head += f"  {f.category}"
        lines.append(head)
        if f.note:
            lines.append(f"    {f.note}")
        if f.value:
            lines.append(f"    value: {f.value!r}  ({len(f.hits)} place(s) in {f.files} file(s))")
        for h in f.hits[:8]:
            lines.append(f"    {h.file} : line {h.line} : ...{h.context}...")
        if len(f.hits) > 8:
            lines.append(f"    ... and {len(f.hits) - 8} more")
        lines.append("")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    data.write_text(json.dumps({
        "build": build(), "matter": str(matter), "at": datetime.now().isoformat(timespec="seconds"),
        "findings": [{"id": f.id, "kind": f.kind, "placeholder": f.placeholder, "names_line": f.names_line,
                      "category": f.category, "value": f.value, "places": len(f.hits)} for f in findings],
    }, indent=1), encoding="utf-8")


def load_findings(private: Path) -> dict[str, dict]:
    path = private / CHECK_JSON
    if not path.is_file():
        raise SafeError("No check results for this matter yet. Run 'redact-local check' first.")
    try:
        return {f["id"]: f for f in json.loads(path.read_text(encoding="utf-8"))["findings"]}
    except (ValueError, KeyError, TypeError):
        raise SafeError("The check results file could not be read. Run 'redact-local check' again.") from None
