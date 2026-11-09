"""Put real values back into a draft that was written with placeholders.

``redact-local restore <draft> --matter <folder>`` reads ``<folder>\\_redact\\key.csv``
and writes ``<draft stem> - restored<ext>`` next to the draft. If the draft is
inside an output folder (``<matter>\\redacted``, or an older ``<matter> - redacted``),
which Claude reads, the restored copy goes to ``<matter>\\_redact\\restored\\`` instead,
so real names never land where Claude looks.

With ``court=True`` it writes ``<draft stem> - court copy<ext>`` for filing instead:
names and the rest come back, but the categories in profiles.toml's [court_filing]
table are cut to their last four characters or withheld (by default Washington
GR 31(e)(1): SSNs and financial account numbers last four only, driver's license
numbers left out).

Supports .txt, .md and .docx drafts. Nothing is printed except counts and the
output path.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from redact_local.errors import SafeError
from redact_local.pipeline import LEGACY_SUFFIX, PRIVATE, is_output_dir
from redact_local.placeholders import PLACEHOLDER_RE, Registry

TEXT_EXT = {".txt", ".md", ".markdown"}


@dataclass
class RestoreResult:
    output: Path
    replaced: int
    distinct: int
    unknown: int
    masked: int = 0      # court copy: occurrences cut to their last four characters
    withheld: int = 0    # court copy: occurrences replaced by the withheld text


def mask_last4(value: str, mask: str = "X") -> str:
    """Every letter and digit except the last four becomes ``mask``; separators stay:
    ``219-44-6012`` -> ``XXX-XX-6012``. A value with four or fewer is masked entirely."""
    positions = [k for k, ch in enumerate(value) if ch.isalnum()]
    hide = set(positions[:-4]) if len(positions) > 4 else set(positions)
    return "".join(mask if k in hide else ch for k, ch in enumerate(value))


def find_key(matter: Path) -> Path:
    """Accept the matter folder, its _redact folder, or its output folder (new or old)."""
    matter = matter.expanduser().resolve()
    candidates = [matter / PRIVATE / "key.csv", matter / "key.csv"]
    if is_output_dir(matter) and not matter.name.endswith(LEGACY_SUFFIX):
        candidates.append(matter.parent / PRIVATE / "key.csv")
    if matter.name.endswith(LEGACY_SUFFIX):
        candidates.append(matter.parent / matter.name[: -len(LEGACY_SUFFIX)] / PRIVATE / "key.csv")
    for c in candidates:
        if c.is_file():
            return c
    raise SafeError("No key.csv found for that matter. Run 'redact-local run' on the matter folder first.")


def _swap(text: str, mapping: dict[str, str], stats: dict, count_unknown: bool = True) -> str:
    def repl(m: re.Match) -> str:
        ph = m.group(1)
        if ph in mapping:
            stats["replaced"] += 1
            stats["seen"].add(ph)
            stats["hits"][ph] += 1
            return mapping[ph]
        if count_unknown and ph not in stats["ignore"]:
            stats["unknown"] += 1
        return m.group(0)

    return PLACEHOLDER_RE.sub(repl, text)


def _output_path(draft: Path, key_path: Path, label: str = "restored") -> Path:
    name = f"{draft.stem} - {label}{draft.suffix}"
    if any(is_output_dir(p) for p in (draft.parent, *draft.parent.parents)):
        out = key_path.parent / "restored"
        out.mkdir(exist_ok=True)
        return out / name
    return draft.with_name(name)


def _runs(par) -> list:
    """Every run in the paragraph, including those inside hyperlinks (par.runs skips them)."""
    if not hasattr(par, "iter_inner_content"):
        return list(par.runs)
    out = []
    for item in par.iter_inner_content():
        out.extend(item.runs if hasattr(item, "runs") else [item])
    return out


def _restore_paragraph(par, mapping, stats) -> None:
    runs = _runs(par)
    # Placeholders inside one run: replace in place, formatting kept.
    for run in runs:
        if "[" in run.text:
            run.text = _swap(run.text, mapping, stats, count_unknown=False)
    # A placeholder split across runs: rebuild the paragraph text in the first run.
    text = "".join(run.text for run in runs)
    if any(m.group(1) in mapping for m in PLACEHOLDER_RE.finditer(text)):
        full = _swap(text, mapping, stats, count_unknown=False)
        for i, run in enumerate(runs):
            run.text = full if i == 0 else ""
        text = full
    stats["unknown"] += sum(1 for m in PLACEHOLDER_RE.finditer(text)
                            if m.group(1) not in mapping and m.group(1) not in stats["ignore"])


def _restore_docx(draft: Path, out: Path, mapping, stats) -> None:
    try:
        import docx
    except ImportError:
        raise SafeError("python-docx is not installed, so .docx drafts can't be restored.") from None
    try:
        d = docx.Document(str(draft))
    except Exception:
        raise SafeError("The .docx draft could not be opened.") from None

    def in_tables(tables):
        for table in tables:
            for row in table.rows:
                for cell in row.cells:
                    yield from cell.paragraphs
                    yield from in_tables(cell.tables)

    def paragraphs():
        yield from d.paragraphs
        yield from in_tables(d.tables)
        for section in d.sections:
            for part in (section.header, section.footer):
                yield from part.paragraphs
                yield from in_tables(part.tables)

    for par in paragraphs():
        _restore_paragraph(par, mapping, stats)
    d.save(str(out))


def restore(draft: str | Path, matter: str | Path, court: bool = False, config=None) -> RestoreResult:
    """``court``: write a court copy, applying profiles.toml's [court_filing] rules
    (read from ``config``, default the repo's config folder)."""
    draft = Path(draft).expanduser()
    if not draft.is_file():
        raise SafeError("Draft file not found.")
    draft = draft.resolve()
    ext = draft.suffix.lower()
    if ext not in TEXT_EXT | {".docx"}:
        raise SafeError("restore supports .txt, .md and .docx drafts.")
    key_path = find_key(Path(matter))
    registry = Registry(key_path)
    mapping = registry.restore_map()
    masked, withheld, ignore = set(), set(), set()
    if court:
        from redact_local.config import config_dir, load_court_filing

        rules = load_court_filing(config_dir(config))
        for ph in list(mapping):
            category = registry.category.get(ph, "")
            if category in rules.withhold:
                mapping[ph] = rules.withheld_text
                withheld.add(ph)
            elif category in rules.last4:
                mapping[ph] = mask_last4(mapping[ph])
                masked.add(ph)
        # "[REDACTED]" looks like a placeholder; it is the withheld text, not one we missed.
        ignore = {m.group(1) for m in PLACEHOLDER_RE.finditer(rules.withheld_text)}
    out = _output_path(draft, key_path, "court copy" if court else "restored")
    stats = {"replaced": 0, "unknown": 0, "seen": set(), "hits": Counter(), "ignore": ignore}
    if ext == ".docx":
        _restore_docx(draft, out, mapping, stats)
    else:
        text = draft.read_text(encoding="utf-8-sig")
        out.write_text(_swap(text, mapping, stats), encoding="utf-8")
    return RestoreResult(out, stats["replaced"], len(stats["seen"]), stats["unknown"],
                         masked=sum(stats["hits"][ph] for ph in masked),
                         withheld=sum(stats["hits"][ph] for ph in withheld))
