r"""One run over a matter folder (or a file or subfolder inside one).

Layout, for a matter folder ``X``:

    X\                    the originals (never modified)
    X\_redact\            private: names.txt, keep.txt, key.csv, review.html,
                          manifest.json, run.log. Claude never reads this.
    X - redacted\         one .txt per source file, names redacted too, plus
                          _summary.json (counts only). Claude reads this after
                          the user has checked review.html.

A file or subfolder is treated as part of its matter (the nearest folder above
it with a _redact folder): same names.txt, same key, output into the same
"- redacted" folder.
"""

from __future__ import annotations

from redact_local import netguard

netguard.install()

import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
from collections import Counter  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

from redact_local import __version__, names, review  # noqa: E402
from redact_local.config import config_dir, load_common_words, load_keep_terms, load_profile  # noqa: E402
from redact_local.errors import SafeError  # noqa: E402
from redact_local.placeholders import Registry  # noqa: E402
from redact_local.textnorm import normalize_text  # noqa: E402

PRIVATE = "_redact"
OUT_SUFFIX = " - redacted"
SKIP_FILES = {"desktop.ini", "thumbs.db", ".ds_store"}

log = logging.getLogger("redact_local.run")


@dataclass
class FileResult:
    source: str                     # path relative to the matter folder (private)
    path: Path
    extracted: object = None
    analysis: object = None
    name_parts: list[str] = field(default_factory=list)
    name_analyses: list = field(default_factory=list)
    output: str | None = None       # output path relative to the output folder
    error: str | None = None        # private: shown in review.html only


@dataclass
class RunResult:
    matter: Path
    out_dir: Path
    private: Path
    review: Path
    summary_path: Path
    log_path: Path
    profile: str
    model: str
    summary: dict
    names_created: bool
    blocked: list[dict]


def matter_paths(path: str | os.PathLike) -> tuple[Path, Path | None]:
    """(matter folder, scope) for an input path.

    The matter is the nearest folder, at or above the input, that already has a
    ``_redact`` folder; failing that, the input folder (or a file's folder). The
    scope is the file or subfolder to process, or None for the whole matter. So
    ``run X/emails`` after ``run X`` reuses X's names.txt and key and writes into
    ``X - redacted``, rather than starting a new, empty matter inside the originals.
    """
    p = Path(path).expanduser()
    if not p.exists():
        raise SafeError("Input path not found.")
    p = p.resolve()
    start = p if p.is_dir() else p.parent
    for part in (start, *start.parents):
        if part.name == PRIVATE or part.name.endswith(OUT_SUFFIX):
            raise SafeError(
                "That path is inside redact-local's private or output folder. "
                "Point at the original matter folder instead."
            )
    matter = next((d for d in (start, *start.parents) if (d / PRIVATE).is_dir()), start)
    return matter, (None if p == matter else p)


def _in_scope(source: str, scope_rel: Path | None) -> bool:
    if scope_rel is None:
        return True
    src = Path(source)
    return src == scope_rel or scope_rel in src.parents


def _walk(root: Path) -> tuple[list[Path], list[Path]]:
    from redact_local.extract import SUPPORTED

    files, skipped = [], []
    for here, dirs, fnames in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != PRIVATE and not d.endswith(OUT_SUFFIX) and not d.startswith("."))
        for fn in sorted(fnames):
            if fn.startswith(("~$", ".")) or fn.lower() in SKIP_FILES:
                continue
            p = Path(here) / fn
            (files if p.suffix.lower() in SUPPORTED else skipped).append(p)
    return files, skipped


def _display(part: str) -> str:
    return normalize_text(re.sub(r"_+", " ", part)).strip()


# Word breaks inside file names: "FakertonLetter", "Fakerton2", "2Fakerton".
_NAME_BREAK = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")


def _analyze_name(engine, part: str):
    """Analyze a file or folder name, also trying it with camelCase and digit runs split.

    Spans found only in the split form are mapped back onto the original, so a
    name that is itself camelCase ("McDonald") still matches as written.
    """
    a = engine.analyze(part)
    cuts = [m.start() for m in _NAME_BREAK.finditer(part)]
    if not cuts:
        return a
    pieces, back, prev = [], [], 0
    for cut in cuts + [len(part)]:
        pieces.append(part[prev:cut])
        back.extend(range(prev, cut))
        if cut < len(part):
            pieces.append(" ")
            back.append(cut)
        prev = cut
    split = engine.analyze("".join(pieces))
    for sp in split.spans:
        start, end = back[sp.start], back[sp.end - 1] + 1
        if any(start < t.end and t.start < end for t in a.spans):
            continue
        sp.start, sp.end, sp.value = start, end, part[start:end]
        a.spans.append(sp)
    a.spans.sort(key=lambda sp: sp.start)
    return a


def _analyze_text(engine, text: str):
    """Analyze extracted text, and each attachment name an email lists the way a file name is.

    To the model "RobertFakename2024.docx" is one token, so a person known only to
    it (not to names.txt) would pass. Spans from the name are kept where nothing
    in the text analysis already covers them.
    """
    from redact_local.extract import ATTACHMENT_NAME_LINE

    a = engine.analyze(text)
    for m in ATTACHMENT_NAME_LINE.finditer(text):
        offset = m.start(1)
        for sp in _analyze_name(engine, m.group(1)).spans:
            start, end = sp.start + offset, sp.end + offset
            if any(start < t.end and t.start < end for t in a.spans):
                continue
            sp.start, sp.end = start, end
            a.spans.append(sp)
    a.spans.sort(key=lambda sp: sp.start)
    return a


_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(name: str) -> str:
    name = _BAD_CHARS.sub("_", name).strip(" .")
    return (name or "file")[:150]


_ANON = None


def render(text: str, spans) -> str:
    """Replace spans with their placeholders using presidio-anonymizer."""
    if not spans:
        return text
    global _ANON
    from presidio_anonymizer import AnonymizerEngine
    from presidio_anonymizer.entities import OperatorConfig, RecognizerResult

    if _ANON is None:
        _ANON = AnonymizerEngine()
    results, ops = [], {}
    for i, s in enumerate(spans):
        key = f"SPAN{i}"
        results.append(RecognizerResult(entity_type=key, start=s.start, end=s.end, score=1.0))
        ops[key] = OperatorConfig("replace", {"new_value": f"[{s.placeholder}]"})
    return _ANON.anonymize(text=text, analyzer_results=results, operators=ops).text


def _link_people(registry: Registry, spans) -> None:
    """Give "Quentin" the same placeholder as "Quentin Fakerton" when that's the only match."""
    by_token: dict[str, set[str]] = {}
    idents = {s.ident for s in spans if s.category == "PERSON"}
    idents |= {i for i in registry.by_ident if i.startswith("PERSON|")}
    for ident in idents:
        toks = ident.split("|", 1)[1].split()
        if len(toks) >= 2:
            for t in {toks[0], toks[-1]}:
                by_token.setdefault(t, set()).add(ident)
    for ident in idents:
        toks = ident.split("|", 1)[1].split()
        if len(toks) == 1 and not registry.known(ident):
            matches = by_token.get(toks[0], set())
            if len(matches) == 1:
                registry.link(ident, next(iter(matches)))


def _open_log(private: Path) -> tuple[Path, logging.Handler]:
    path = private / "run.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("redact_local")
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False
    return path, handler


def run(path: str | os.PathLike, profile_name: str = "client", config: str | os.PathLike | None = None) -> RunResult:
    matter, scope = matter_paths(path)
    private = matter / PRIVATE
    private.mkdir(exist_ok=True)
    out_dir = matter.parent / f"{matter.name}{OUT_SUFFIX}"
    log_path, handler = _open_log(private)
    try:
        return _run(matter, scope, private, out_dir, log_path, profile_name, config)
    finally:
        logging.getLogger("redact_local").removeHandler(handler)
        handler.close()


def _run(matter, scope, private, out_dir, log_path, profile_name, config) -> RunResult:
    from redact_local.detect import Engine
    from redact_local.extract import extract

    cfg = config_dir(config)
    profile = load_profile(profile_name, cfg)
    log.info("run started: profile=%s model=%s", profile.name, profile.spacy_model)

    names_path = private / "names.txt"
    names_created = not names_path.exists()
    if names_created:
        names.write_template(names_path)
    registry = Registry(private / "key.csv")
    names_file = names.parse(names_path, existing=registry.primary)
    keep_terms = load_keep_terms(profile, cfg, private)
    engine = Engine(profile, keep_terms, load_common_words(cfg), names_file)

    scope_rel = scope.relative_to(matter) if scope is not None else None
    if scope is not None and scope.is_file():
        from redact_local.extract import SUPPORTED

        if scope.suffix.lower() not in SUPPORTED:
            raise SafeError("Unsupported file type.")
        files, skipped = [scope], []
    else:
        files, skipped = _walk(scope or matter)

    # Pass 1: extract and detect.
    results: list[FileResult] = []
    for p in files:
        rel = p.relative_to(matter)
        fr = FileResult(source=str(rel), path=p)
        fr.name_parts = [_display(part) for part in rel.parent.parts] + [_display(rel.stem)]
        fr.name_analyses = [_analyze_name(engine, part) for part in fr.name_parts]
        try:
            fr.extracted = extract(p)
            fr.analysis = _analyze_text(engine, fr.extracted.text)
        except SafeError as exc:
            fr.error = str(exc)
            log.warning("file failed: %s: %s", rel, exc)
        except Exception as exc:  # keep going; details only in the private log
            fr.error = f"{type(exc).__name__} (details in run.log)"
            log.exception("file failed: %s", rel)
        results.append(fr)

    # Pass 2: placeholders, consistent across files and with earlier runs.
    all_spans = [s for fr in results for a in ([*fr.name_analyses, fr.analysis] if fr.analysis else fr.name_analyses) for s in a.spans]
    _link_people(registry, all_spans)
    for fr in results:
        for a in [*fr.name_analyses, *([fr.analysis] if fr.analysis else [])]:
            for s in a.spans:
                s.placeholder = registry.assign(s.ident, s.category, a.text[s.start:s.end], fixed=s.fixed, primary=s.primary)

    # Pass 3: write outputs. key.csv and the manifest are saved even if this
    # pass fails partway, so restore works and the next run can clean up.
    out_dir.mkdir(exist_ok=True)
    manifest_path = private / "manifest.json"
    manifest: dict[str, str] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")).get("outputs", {})
        except (ValueError, AttributeError):
            manifest = {}
    stale_left = 0
    try:
        # Remove this scope's previous outputs: a rerun may name them differently
        # (a name newly added to names.txt), and the old name must not linger.
        out_root = out_dir.resolve()
        for source, rel in list(manifest.items()):
            if not _in_scope(source, scope_rel):
                continue
            target = (out_dir / rel).resolve()
            try:
                if target.is_file() and out_root in target.parents:
                    target.unlink()
                del manifest[source]
            except OSError:
                stale_left += 1
                log.exception("could not remove an earlier output: %s", rel)
        _prune_empty_dirs(out_dir)

        used = {v.casefold() for v in manifest.values()}
        for fr in results:
            if fr.analysis is None:
                continue
            parts = [_safe_name(render(a.text, a.spans)) for a in fr.name_analyses]
            ext = fr.path.suffix.lower().lstrip(".")
            base = Path(*parts[:-1]) if len(parts) > 1 else Path()
            candidate = base / f"{parts[-1]}.{ext}.txt"
            n = 2
            while str(candidate).casefold() in used:
                candidate = base / f"{parts[-1]} ({n}).{ext}.txt"
                n += 1
            try:
                body = render(fr.analysis.text, fr.analysis.spans)
                target = out_dir / candidate
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(_header(profile.name, fr) + body.rstrip() + "\n", encoding="utf-8")
            except OSError:
                fr.error = "The output file could not be written (details in run.log)."
                log.exception("could not write output for %s", fr.source)
                continue
            used.add(str(candidate).casefold())
            fr.output = str(candidate)
            manifest[fr.source] = fr.output
    finally:
        registry.save()
        manifest_path.write_text(json.dumps({"outputs": manifest}, indent=1), encoding="utf-8")

    blocked = netguard.attempts()
    for a in blocked:
        log.error("network attempt blocked: module=%s via=%s call=%s target=%s", a["module"], a["via"], a["call"], a["target"])
    summary = _summary(profile, results, skipped, registry, blocked)
    summary["stale_outputs_not_removed"] = stale_left
    summary_path = out_dir / "_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    review_path = private / "review.html"
    review.write(
        review_path, matter=matter, out_dir=out_dir, profile=profile, files=results,
        skipped=[str(p.relative_to(matter)) for p in skipped],
        names_info={"entries": len(names_file.entries), "created": names_created, "bad_lines": names_file.bad_lines},
        keep_count=len(keep_terms),
        totals={
            "files": summary["files"]["processed"], "skipped": summary["files"]["skipped"],
            "failed": summary["files"]["failed"], "attachments_failed": summary["files"]["attachments_failed"],
            "redactions": summary["redactions"]["total"],
            "distinct": summary["redactions"]["distinct_values"], "misses": summary["possible_misses"],
            "blocked": len(blocked),
        },
    )
    log.info("run finished: %s", json.dumps(summary["files"]))
    return RunResult(
        matter=matter, out_dir=out_dir, private=private, review=review_path, summary_path=summary_path,
        log_path=log_path, profile=profile.name, model=profile.spacy_model, summary=summary,
        names_created=names_created,
        blocked=[{"module": a["module"], "call": a["call"]} for a in blocked],
    )


def _header(profile: str, fr: FileResult) -> str:
    ex = fr.extracted
    how = ex.kind
    if ex.kind == "pdf":
        how += f", {ex.pages} page(s)" + (f", {ex.ocr_pages} OCR'd" if ex.ocr_pages else "")
    elif ex.kind == "image":
        how += ", OCR"
    return f"[redact-local | profile: {profile} | source: {how} | {len(fr.analysis.spans)} redaction(s)]\n\n"


def _prune_empty_dirs(root: Path) -> None:
    for dirpath, _dirs, _files in os.walk(root, topdown=False):
        p = Path(dirpath)
        if p != root and not any(p.iterdir()):
            p.rmdir()


def _summary(profile, results: list[FileResult], skipped, registry: Registry, blocked) -> dict:
    """Counts only. This file sits in the output folder, which Claude reads."""
    ok = [fr for fr in results if fr.analysis is not None and fr.error is None]
    by_type = Counter(fr.extracted.kind for fr in ok)
    by_cat: Counter = Counter()
    distinct = set()
    keep_hits = kept = protected = misses = 0
    for fr in ok:
        for a in [*fr.name_analyses, fr.analysis]:
            for s in a.spans:
                by_cat["NAMES.TXT" if s.category == "NAME" else s.category] += 1
                distinct.add(s.placeholder)
            keep_hits += len(a.keep_hits)
            kept += sum(1 for k in a.kept if not k.reason.startswith("policy"))
            protected += len(a.protected)
            misses += len(a.misses)
    return {
        "tool": "redact-local",
        "version": __version__,
        "profile": profile.name,
        "spacy_model": profile.spacy_model,
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": {
            "processed": len(ok),
            "by_type": dict(sorted(by_type.items())),
            "skipped": len(skipped),
            "failed": len(results) - len(ok),
            # Files attached to a PDF (read, or failed) and attachments not opened by design
            # (an unsupported type, or attached to an email).
            "attachments_read": sum(fr.extracted.attachments for fr in ok),
            "attachments_failed": sum(fr.extracted.attachments_failed for fr in ok),
            "attachments_not_processed": sum(fr.extracted.attachments_skipped for fr in ok),
        },
        "pages": {
            "pdf_pages": sum(fr.extracted.pages for fr in ok if fr.extracted.kind == "pdf"),
            "pdf_text_layer": sum(fr.extracted.text_pages for fr in ok),
            "pdf_ocr": sum(fr.extracted.ocr_pages for fr in ok if fr.extracted.kind == "pdf"),
            "embedded_images_ocr": sum(fr.extracted.ocr_regions for fr in ok),
            "images_ocr": sum(fr.extracted.images_ocr for fr in ok),
        },
        "redactions": {
            "total": sum(by_cat.values()),
            "distinct_values": len(distinct),
            "by_category": dict(sorted(by_cat.items())),
        },
        "kept": {"keep_list_occurrences": keep_hits, "kept_by_rule": kept, "protected_citations": protected},
        "possible_misses": misses,
        "network_attempts_blocked": len(blocked),
    }
