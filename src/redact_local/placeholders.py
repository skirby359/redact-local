"""Placeholders and the per-matter key (``_redact/key.csv``).

The key maps each placeholder back to the original value. It persists across
runs, so ``[PERSON-3]`` means the same person in every file and every rerun,
and drafts written against an earlier run still restore correctly.

key.csv has one row per spelling seen:
    placeholder  PERSON-3 (no brackets)
    category     PERSON, EMAIL, NAME (from names.txt), ...
    value        the spelling as it appeared
    ident        the identity key that spelling maps from
    primary      "yes" on the row ``restore`` puts back
"""

from __future__ import annotations

import csv
import os
import re
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

PREFIX = {
    "PERSON": "PERSON", "EMAIL": "EMAIL", "PHONE": "PHONE", "HANDLE": "HANDLE",
    "SSN": "SSN", "DRIVER-LICENSE": "DRIVER-LICENSE", "CARD": "CARD", "IBAN": "IBAN",
    "IP": "IP", "ADDRESS": "ADDRESS", "DOB": "DOB", "TICKET": "TICKET",
    "LOCATION": "PLACE", "ORG": "ORG", "DATE": "DATE", "NRP": "GROUP",
}
FIELDS = ["placeholder", "category", "value", "ident", "primary"]
PLACEHOLDER_RE = re.compile(r"\[([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*)\]")
_NUMBERED = re.compile(r"^(.*)-(\d+)$")


class Registry:
    def __init__(self, path: Path):
        self.path = path
        self.by_ident: dict[str, str] = {}
        self.category: dict[str, str] = {}
        self.primary: dict[str, str] = {}
        self.rows: dict[str, dict[str, str]] = defaultdict(dict)  # placeholder -> {value: ident}
        self.links: dict[str, str] = {}
        self.new: set[str] = set()
        self._seen_now: dict[str, Counter] = defaultdict(Counter)
        self._fixed_primary: dict[str, str] = {}
        self._load()

    # ── persistence ──

    def _load(self) -> None:
        if not self.path.is_file():
            return
        with self.path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                ph = (row.get("placeholder") or "").strip().strip("[]")
                value = row.get("value") or ""
                if not ph or not value:
                    continue
                ident = row.get("ident") or ""
                self.category.setdefault(ph, row.get("category") or "")
                self.rows[ph].setdefault(value, ident)
                if ident:
                    self.by_ident.setdefault(ident, ph)
                if (row.get("primary") or "").lower() == "yes":
                    self.primary[ph] = value

    def save(self) -> None:
        # Settle primaries: the names.txt spelling wins; otherwise an existing
        # primary stays; a new placeholder takes its most common spelling.
        for ph, value in self._fixed_primary.items():
            self.primary[ph] = value
            self.rows[ph].setdefault(value, f"NAMES|{ph}")
        for ph in self.new:
            if ph not in self.primary and self._seen_now[ph]:
                self.primary[ph] = self._seen_now[ph].most_common(1)[0][0]

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="key-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=FIELDS)
                w.writeheader()
                for ph in sorted(self.rows, key=_sort_key):
                    for value, ident in self.rows[ph].items():
                        w.writerow({
                            "placeholder": ph,
                            "category": self.category.get(ph, ""),
                            "value": value,
                            "ident": ident,
                            "primary": "yes" if value == self.primary.get(ph) else "",
                        })
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ── assignment ──

    def known(self, ident: str) -> bool:
        return ident in self.by_ident

    def link(self, from_ident: str, to_ident: str) -> None:
        """Let one identity share another's placeholder ("Jane" -> "Jane Example")."""
        if from_ident != to_ident and from_ident not in self.by_ident:
            self.links[from_ident] = to_ident

    def _next(self, prefix: str) -> str:
        n = 1
        while f"{prefix}-{n}" in self.rows or f"{prefix}-{n}" in self.new:
            n += 1
        return f"{prefix}-{n}"

    def assign(self, ident: str, category: str, value: str, fixed: str | None = None, primary: str | None = None) -> str:
        """Placeholder (without brackets) for this value, creating one if needed."""
        if fixed:
            ph = fixed
            self.by_ident.setdefault(ident, ph)
        else:
            ph = self.by_ident.get(ident) or self.by_ident.get(self.links.get(ident, ""))
            if ph is None:
                ph = self._next(PREFIX.get(category, category))
                self.by_ident[ident] = ph
        if ph not in self.rows:
            self.new.add(ph)
        self.category[ph] = category
        self.rows[ph].setdefault(value, ident)
        self.by_ident.setdefault(ident, ph)
        self._seen_now[ph][value] += 1
        if primary:
            self._fixed_primary[ph] = primary
        return ph

    def restore_map(self) -> dict[str, str]:
        out = {}
        for ph, values in self.rows.items():
            value = self.primary.get(ph) or next(iter(values), None)
            if value:
                out[ph] = value
        return out


def _sort_key(ph: str):
    m = _NUMBERED.match(ph)
    return (m.group(1), int(m.group(2))) if m else (ph, 0)


def load_restore_map(path: Path) -> dict[str, str]:
    return Registry(path).restore_map()
