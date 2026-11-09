"""Profiles, keep lists and word lists from the config folder."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from redact_local.errors import UsageError

# src/redact_local/config.py -> repo root -> config/
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

ALL_CATEGORIES = {
    "PERSON", "EMAIL", "PHONE", "HANDLE", "SSN", "DRIVER-LICENSE", "CARD",
    "IBAN", "IP", "ADDRESS", "DOB", "TICKET", "VIN", "PLATE", "LOCATION", "ORG", "DATE", "NRP",
}
NAME_VARIANTS = {"full", "last-first", "initial-last", "first", "last"}


@dataclass
class Profile:
    name: str
    description: str
    spacy_model: str
    score_threshold: float
    redact: frozenset[str]
    keep_files: list[str]
    name_variants: list[str]
    protect_citation_names: bool
    protect_case_numbers: bool
    keep_titled_officials: bool
    official_titles: list[str] = field(default_factory=list)
    redact_possessive_names: bool = False


def config_dir(override: str | os.PathLike | None = None) -> Path:
    path = Path(override) if override else Path(os.environ.get("REDACT_LOCAL_CONFIG", DEFAULT_CONFIG_DIR))
    if not (path / "profiles.toml").is_file():
        raise UsageError(
            f"No profiles.toml in config folder: {path}. redact-local reads its config from a clone "
            "of the repository: install it with 'pip install -e <clone>', or pass --config <folder> "
            "(or set REDACT_LOCAL_CONFIG)."
        )
    return path


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise UsageError(f"{path.name} is not valid TOML (line {getattr(exc, 'lineno', '?')}).") from None


def list_profiles(cfg: Path) -> dict[str, str]:
    data = _read_toml(cfg / "profiles.toml")
    return {name: body.get("description", "") for name, body in data.get("profiles", {}).items()}


def load_profile(name: str, cfg: Path) -> Profile:
    data = _read_toml(cfg / "profiles.toml")
    profiles = data.get("profiles", {})
    if name not in profiles:
        raise UsageError(f"Unknown profile '{name}'. Available: {', '.join(sorted(profiles))}.")
    merged = dict(data.get("defaults", {}))
    merged.update(profiles[name])

    redact = frozenset(str(c).upper() for c in merged.get("redact", []))
    unknown = redact - ALL_CATEGORIES
    if unknown:
        raise UsageError(f"profiles.toml: unknown categories in 'redact': {', '.join(sorted(unknown))}.")
    variants = [str(v) for v in merged.get("name_variants", ["full"])]
    bad = set(variants) - NAME_VARIANTS
    if bad:
        raise UsageError(f"profiles.toml: unknown name_variants: {', '.join(sorted(bad))}.")

    return Profile(
        name=name,
        description=str(merged.get("description", "")),
        spacy_model=str(merged.get("spacy_model", "en_core_web_lg")),
        score_threshold=float(merged.get("score_threshold", 0.4)),
        redact=redact,
        keep_files=[str(f) for f in merged.get("keep_files", ["keep.txt"])],
        name_variants=variants,
        protect_citation_names=bool(merged.get("protect_citation_names", False)),
        protect_case_numbers=bool(merged.get("protect_case_numbers", False)),
        keep_titled_officials=bool(merged.get("keep_titled_officials", False)),
        official_titles=[str(t) for t in merged.get("official_titles", [])],
        redact_possessive_names=bool(merged.get("redact_possessive_names", False)),
    )


@dataclass
class CourtFiling:
    """What ``restore --court`` does with each category (the [court_filing] table)."""
    last4: frozenset[str]
    withhold: frozenset[str]
    withheld_text: str


# Washington GR 31(e)(1): SSNs and financial account numbers only as their last four
# digits; driver's license numbers left out. Used when profiles.toml has no [court_filing].
COURT_FILING_DEFAULTS = {"last4": ["SSN", "CARD", "IBAN"], "withhold": ["DRIVER-LICENSE"],
                         "withheld_text": "[REDACTED]"}


def load_court_filing(cfg: Path) -> CourtFiling:
    data = _read_toml(cfg / "profiles.toml").get("court_filing", {})
    merged = {**COURT_FILING_DEFAULTS, **data}
    last4 = frozenset(str(c).upper() for c in merged["last4"])
    withhold = frozenset(str(c).upper() for c in merged["withhold"])
    unknown = (last4 | withhold) - ALL_CATEGORIES - {"NAME"}
    if unknown:
        raise UsageError(f"profiles.toml [court_filing]: unknown categories: {', '.join(sorted(unknown))}.")
    both = last4 & withhold
    if both:
        raise UsageError(f"profiles.toml [court_filing]: listed under both last4 and withhold: "
                         f"{', '.join(sorted(both))}.")
    text = str(merged["withheld_text"])
    if not text.strip():
        raise UsageError("profiles.toml [court_filing]: withheld_text must not be empty.")
    return CourtFiling(last4, withhold, text)


def read_list_file(path: Path) -> list[str]:
    """One entry per line; blank lines and lines starting with # are skipped."""
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def load_keep_terms(profile: Profile, cfg: Path, matter_private: Path | None) -> list[str]:
    terms: list[str] = []
    for fname in profile.keep_files:
        path = cfg / fname
        if not path.is_file():
            raise UsageError(f"Keep list named in profiles.toml not found: {path}")
        terms.extend(read_list_file(path))
    # Personal additions that aren't committed (config/*.local.txt is gitignored).
    terms.extend(read_list_file(cfg / "keep.local.txt"))
    if matter_private is not None:
        terms.extend(read_list_file(matter_private / "keep.txt"))
    # De-duplicate, keep order.
    seen, out = set(), []
    for t in terms:
        key = normalize_term(t)
        if key and key not in seen:
            seen.add(key)
            out.append(t)
    return out


def load_common_words(cfg: Path) -> set[str]:
    return {w.lower() for w in read_list_file(cfg / "common-words.txt")}


_WS = re.compile(r"\s+")


def normalize_term(term: str) -> str:
    """Case-fold, drop periods, collapse whitespace: the key used to compare terms."""
    return _WS.sub(" ", term.replace(".", "").strip()).casefold()


def flexible_term_body(term: str) -> str:
    """Regex body for a keep-list term: flexible spaces, optional periods and commas.

    "GitHub, Inc." also matches "GitHub Inc." and "GITHUB, INC" (use re.I).
    """
    parts = [re.escape(token).replace(r"\.", r"\.?").replace(",", ",?") for token in term.split()]
    return r"\s+".join(parts)


def term_is_case_sensitive(term: str) -> bool:
    """All-caps terms ("EFF", "U.S.", "AGO", "DOC") must not match "us", "ago", "doc"."""
    return not any(c.islower() for c in term)


def flexible_term_regex(term: str) -> str:
    """Keep-list term as a standalone match (not inside a longer word or email).

    Compile with re.IGNORECASE; all-caps terms carry their own (?-i:) scope.
    """
    body = flexible_term_body(term)
    if term_is_case_sensitive(term):
        body = f"(?-i:{body})"
    return rf"(?<![\w@]){body}(?![\w@])"
