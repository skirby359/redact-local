"""Command line for redact-local.

    redact-local run <path> [--profile client|court-records] [--suggest-names]
    redact-local roles <folder> PERSON-4=OWNER PERSON-9=KEEP ...
    redact-local restore <draft> --matter <folder> [--court]
    redact-local check <folder>
    redact-local where <folder> PERSON-4 ...
    redact-local adopt <folder> F3 F7 ...
    redact-local profiles

The console only ever shows counts and paths. While the work runs, anything a
library prints (from Python or native code) goes to the private
``_redact\\run.log`` instead, and errors are reported by type, never by message,
unless they are our own SafeErrors (whose wording is fixed).
"""

from __future__ import annotations

from redact_local import netguard

netguard.install()

import argparse  # noqa: E402
import contextlib  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import traceback  # noqa: E402
from pathlib import Path  # noqa: E402

from redact_local import build  # noqa: E402
from redact_local.errors import SafeError  # noqa: E402

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_NETWORK, EXIT_PARTIAL, EXIT_FINDINGS = 0, 1, 2, 3, 4, 5


@contextlib.contextmanager
def _quiet(log_path: Path | None):
    """Route stdout/stderr, Python-level and fd-level, to the private log."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()
    target = open(log_path, "a", encoding="utf-8") if log_path else open(os.devnull, "w", encoding="utf-8")
    saved: list[tuple[int, int]] = []
    try:
        for fd in (1, 2):
            try:
                dup = os.dup(fd)
            except OSError:
                continue
            os.dup2(target.fileno(), fd)
            saved.append((fd, dup))
        with contextlib.redirect_stdout(target), contextlib.redirect_stderr(target):
            yield target
    finally:
        with contextlib.suppress(Exception):
            target.flush()
        for fd, dup in saved:
            os.dup2(dup, fd)
            os.close(dup)
        target.close()


def _log_traceback(log_path: Path | None) -> None:
    if not log_path:
        return
    with contextlib.suppress(Exception), open(log_path, "a", encoding="utf-8") as fh:
        fh.write(traceback.format_exc())


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _print_blocked(blocked: list[dict]) -> None:
    modules = sorted({b["module"] for b in blocked})
    print(f"NETWORK: blocked {_plural(len(blocked), 'connection attempt')} from: {', '.join(modules)}.")
    print("  Nothing was sent. Outputs were written, but treat this run as failed and find out why")
    print("  before trusting the tool. Details (incl. target) are in the private run.log.")


def cmd_run(args) -> int:
    from redact_local import pipeline

    matter, _single = pipeline.matter_paths(args.path)
    private = matter / pipeline.PRIVATE
    private.mkdir(exist_ok=True)
    log_path = private / "run.log"
    with _quiet(log_path):
        try:
            result = pipeline.run(args.path, args.profile, args.config, suggest_names=args.suggest_names)
        except SafeError:
            raise
        except BaseException:
            _log_traceback(log_path)
            raise

    s = result.summary
    f, pg, r, k = s["files"], s["pages"], s["redactions"], s["kept"]
    types = ", ".join(f"{t} {n}" for t, n in f["by_type"].items()) or "none"
    print(f"redact-local {build()} | profile: {result.profile} | model: {result.model}")
    print(f"Files: {f['processed']} processed ({types}); {f['skipped']} skipped (unsupported type); {f['failed']} failed")
    read = []
    if pg["pdf_pages"]:
        read.append(f"PDF pages {pg['pdf_pages']} ({pg['pdf_text_layer']} text layer, {pg['pdf_ocr']} OCR)")
    if pg["embedded_images_ocr"]:
        read.append(f"{_plural(pg['embedded_images_ocr'], 'embedded image')} OCR")
    if pg["images_ocr"]:
        read.append(f"{_plural(pg['images_ocr'], 'image file')} OCR")
    if f["attachments_read"]:
        read.append(_plural(f["attachments_read"], "file attached to a PDF", "files attached to PDFs"))
    if read:
        print("Read: " + "; ".join(read))
    cats = ", ".join(f"{c} {n}" for c, n in sorted(r["by_category"].items(), key=lambda kv: -kv[1]))
    print(f"Redactions: {r['total']} ({r['distinct_values']} distinct)" + (f": {cats}" if cats else ""))
    print(f"Kept: {k['keep_list_occurrences']} keep-list occurrence(s), {k['kept_by_rule']} by rule, "
          f"{k['protected_citations']} protected citation span(s)")
    print(f"Possible misses to check: {s['possible_misses']}")
    names_txt = result.private / "names.txt"
    found = _plural(result.names_suggested, "person", "people")
    uncomment = "  Uncomment the real ones (a role after => is optional), delete the rest, then rerun."
    if result.names_created:
        print(f"names.txt: none yet, so one was created at {names_txt}")
        if result.names_suggested:
            print(f"  It lists {found} the scan found, commented out.")
            print(uncomment)
        else:
            print("  Add the people to remove, then rerun.")
    elif result.names_suggested:
        print(f"names.txt: added {found} the scan found, commented out, to {names_txt}")
        print(uncomment)
    elif args.suggest_names and not result.names_suggest_failed:
        print("names.txt: no new people to suggest.")
    if result.names_suggest_failed:
        print("names.txt: suggestions could not be added (details in the private run.log).")
    if f["attachments_not_processed"]:
        print(f"Attachments not opened: {f['attachments_not_processed']} (unsupported type, or attached to an email; "
              "see the Files table in the review sheet)")
    if f["failed"]:
        print(f"{_plural(f['failed'], 'file')} could not be read; see the Files table in the review sheet.")
    if f["attachments_failed"]:
        print(f"{_plural(f['attachments_failed'], 'file attached to a PDF', 'files attached to PDFs')} could not be "
              "read; see the Files table in the review sheet.")
    if s.get("stale_outputs_not_removed"):
        print(f"WARNING: {_plural(s['stale_outputs_not_removed'], 'earlier output file')} could not be removed "
              "(locked or read-only). It may carry an older, less-redacted name. Close it and rerun "
              "before anyone reads the output folder.")
    print(f"Network guard: on, {_plural(s['network_attempts_blocked'], 'attempt')} blocked")
    print()
    print(f"Matter folder: {result.matter}")
    print(f"Output folder: {result.out_dir}")
    print(f"Review sheet:  {result.review}")
    print("  The review sheet contains the original values. Open it yourself; Claude must not read it.")
    print(f'Before anyone reads the output: redact-local check "{result.matter}"')
    if result.legacy_out:
        print(f"Older output folder (before 0.2.0), no longer updated: {result.legacy_out}")
        print("  Delete it once you no longer need it, so nobody reads stale output.")

    if result.blocked:
        _print_blocked(result.blocked)
        return EXIT_NETWORK
    return EXIT_PARTIAL if f["failed"] or f["attachments_failed"] or s.get("stale_outputs_not_removed") else EXIT_OK


def cmd_restore(args) -> int:
    from redact_local import restore

    key = restore.find_key(Path(args.matter))
    log_path = key.parent / "run.log"
    with _quiet(log_path):
        try:
            result = restore.restore(args.draft, args.matter, court=args.court, config=args.config)
        except SafeError:
            raise
        except BaseException:
            _log_traceback(log_path)
            raise
    print(f"Restored {_plural(result.replaced, 'placeholder')} ({result.distinct} distinct); "
          f"{result.unknown} not in the key, left as they were.")
    if args.court:
        print(f"Court copy: {result.masked} cut to the last four characters, {result.withheld} withheld "
              "(rules: [court_filing] in profiles.toml).")
        if result.unknown:
            print(f"  WARNING: {_plural(result.unknown, 'placeholder')} not in the key "
                  f"{'is' if result.unknown == 1 else 'are'} still in the text. Fix the draft before filing.")
    print(f"Output: {result.output}")
    print("  It contains the real values. Open it yourself; Claude must not read it.")
    return EXIT_OK


def cmd_roles(args) -> int:
    from redact_local import names, pipeline
    from redact_local.errors import UsageError
    from redact_local.placeholders import Registry

    matter, _scope = pipeline.matter_paths(args.matter)
    private = matter / pipeline.PRIVATE
    names_path = private / "names.txt"
    if not names_path.is_file():
        raise UsageError("This matter has no names.txt yet. Run it first.")
    assignments = []
    for item in args.assignments:
        if "=" not in item:
            raise UsageError("Give each role as PLACEHOLDER=ROLE, e.g. PERSON-4=OWNER or PERSON-9=KEEP.")
        ph, role = item.split("=", 1)
        assignments.append((ph, role))
    from redact_local.detect import ident_for

    reg = Registry(private / "key.csv")
    spellings = {ph: [v for v in [reg.primary.get(ph), *values] if v] for ph, values in reg.rows.items()}
    # Which placeholder each active line produces now (a line's "# [PERSON-n]" note can be stale).
    current = {}
    for e in names.parse(names_path).entries:
        ph = e.placeholder_base or reg.by_ident.get(ident_for("PERSON", e.canonical))
        if ph:
            current[e.line_no] = ph
    result = names.set_roles(names_path, assignments, spellings, current)

    print(f"names.txt: {_plural(result.roles, 'role')} set, {result.kept} marked KEEP (not a person), "
          f"{result.plain} made active without a role"
          + (f"; {_plural(result.merged, 'line')} merged into another (same person)" if result.merged else "")
          + (f"; {_plural(result.added, 'person', 'people')} added from the key" if result.added else ""))
    print(f"  Backup of the previous names.txt: {names_path.with_suffix('.txt.bak')}")
    print(f"  Changed line(s): {', '.join(map(str, result.lines)) or 'none'}")
    if result.merged:
        print("  Lines below a merge have moved up. Use 'redact-local where' for a placeholder's line now.")
    print(f"  File: {names_path}")
    print(f'Rerun to apply: redact-local run "{matter}"')
    return EXIT_OK


def cmd_check(args) -> int:
    from redact_local import check, pipeline

    matter, _scope = pipeline.matter_paths(args.matter)
    log_path = matter / pipeline.PRIVATE / "run.log"
    with _quiet(log_path if log_path.parent.is_dir() else None):
        try:
            result = check.run_check(args.matter, args.config)
        except SafeError:
            raise
        except BaseException:
            _log_traceback(log_path)
            raise
    print(f"redact-local {build()} | check")
    print(f"Output files checked: {result.files}")
    labels = [("known", "Known values still in the output"), ("neighbour", "Capitalized words next to a person's placeholder"),
              ("word", "Words of a person's name on their own"), ("shape", "Identifier shapes"),
              ("names", "names.txt problems")]
    for kind, label in labels:
        fs = result.of(kind)
        extra = ""
        if kind == "shape" and fs:
            cats: dict[str, int] = {}
            for f in fs:
                cats[f.category] = cats.get(f.category, 0) + 1
            extra = " (" + ", ".join(f"{c} {n}" for c, n in sorted(cats.items())) + ")"
        print(f"{label}: {len(fs)}{extra}")
    print(f"Skipped as ordinary words, keep-list terms or form labels: {_plural(result.skipped_values, 'value')}")
    if not result.findings:
        print("Nothing found. That lowers the risk; it doesn't prove the output is clean.")
    else:
        print("Findings (ids and places only; the text is in the private report):")
        for f in result.findings[:60]:
            if f.kind == "names":
                print(f"  {f.id:<4} {f.note}")
                continue
            who = f" [{f.placeholder}]" if f.placeholder else ""
            line = f", names.txt line {f.names_line}" if f.names_line else ""
            cat = f" {f.category}" if f.kind == "shape" else ""
            print(f"  {f.id:<4} {check._KIND_TEXT[f.kind]}{cat}{who}{line}: "
                  f"{_plural(len(f.hits), 'place')} in {_plural(f.files, 'file')}")
        if len(result.findings) > 60:
            print(f"  ... and {len(result.findings) - 60} more in the report.")
    print(f"Report: {result.report}")
    print("  It contains the original values. Open it yourself; Claude must not read it.")
    if any(f.kind in {"known", "word", "neighbour"} and f.placeholder for f in result.findings):
        print(f'Add a finding\'s word to that person\'s names.txt line: redact-local adopt "{result.matter}" <id> ...')
    return EXIT_FINDINGS if result.findings else EXIT_OK


def _names_context(matter_arg):
    from redact_local import check, pipeline
    from redact_local.errors import UsageError
    from redact_local.placeholders import Registry

    matter, _scope = pipeline.matter_paths(matter_arg)
    private = matter / pipeline.PRIVATE
    names_path = private / "names.txt"
    if not names_path.is_file():
        raise UsageError("This matter has no names.txt yet. Run it first.")
    reg = Registry(private / "key.csv")
    return matter, private, names_path, check.key_spellings(reg), check.current_lines(names_path, reg)


def cmd_where(args) -> int:
    from redact_local import names

    _matter, _private, names_path, spellings, current = _names_context(args.matter)
    for ph, loc in names.where(names_path, args.placeholders, spellings, current).items():
        if loc.lines:
            role = f", {loc.role}" if loc.role else ""
            print(f"{ph}: names.txt line {', '.join(map(str, loc.lines))} ({loc.state}{role})")
        elif loc.state == "key only":
            print(f"{ph}: in the key, but on no names.txt line ('roles' or 'adopt' adds one)")
        else:
            print(f"{ph}: not in names.txt or the key")
    return EXIT_OK


_CONTACT = re.compile(r"-(?:EMAIL|PHONE|HANDLE)(?:-\d+)?$")


def cmd_adopt(args) -> int:
    from redact_local import check, names
    from redact_local.errors import UsageError

    matter, private, names_path, spellings, current = _names_context(args.matter)
    findings = check.load_findings(private)
    additions = []
    for raw in args.ids:
        fid = raw.strip().upper()
        f = findings.get(fid)
        if f is None:
            raise UsageError(f"No finding {fid} in the last check. Run 'redact-local check' again.")
        ph = _CONTACT.sub("", f.get("placeholder") or "")
        if f.get("kind") not in {"known", "word", "neighbour"} or not ph or not (
                f.get("category") in {"PERSON", "NAME"} or ph != f.get("placeholder")):
            raise UsageError(f"{fid} isn't tied to a person, so there is no names.txt line to add it to. "
                             "Check it in the report; if it is personal data, add it to names.txt yourself.")
        additions.append((ph, f.get("value") or ""))
    result = names.add_spellings(names_path, additions, spellings, current)
    print(f"names.txt: {_plural(result.added, 'spelling')} added, {_plural(result.activated, 'line')} made active"
          + (f", {_plural(result.created, 'line')} added for people only in the key" if result.created else ""))
    print(f"  Backup of the previous names.txt: {names_path.with_suffix('.txt.bak')}")
    print(f"  Changed line(s): {', '.join(map(str, result.lines)) or 'none'}")
    print(f"  File: {names_path}")
    print(f'Rerun to apply, then check again: redact-local run "{matter}"')
    return EXIT_OK


def cmd_profiles(args) -> int:
    from redact_local.config import config_dir, list_profiles

    for name, desc in list_profiles(config_dir(args.config)).items():
        print(f"{name}: {desc}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="redact-local", description="De-identify matter documents on this machine.")
    p.add_argument("--version", action="version", version=f"redact-local {build()}")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="Redact a matter folder (or one file in it).")
    r.add_argument("path", help="Matter folder, or a single file inside one.")
    r.add_argument("--profile", default="client", help="Profile from config/profiles.toml (default: client).")
    r.add_argument("--config", default=None, help="Config folder (default: the repo's config/).")
    r.add_argument("--suggest-names", action="store_true",
                   help="Add people this run found to names.txt, commented out, if they aren't on it yet "
                        "(done anyway while names.txt is still the blank template).")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("restore", help="Swap placeholders in a draft back to the real values.")
    s.add_argument("draft", help="Draft written with placeholders (.txt, .md or .docx).")
    s.add_argument("--matter", required=True, help="The matter folder the placeholders came from.")
    s.add_argument("--court", action="store_true",
                   help="Write a court copy for filing: SSNs and account numbers cut to the last four, "
                        "driver's license numbers withheld (set in [court_filing] in profiles.toml).")
    s.add_argument("--config", default=None, help="Config folder (default: the repo's config/).")
    s.set_defaults(func=cmd_restore)

    ro = sub.add_parser(
        "roles", help="Set roles in names.txt by placeholder (PERSON-4=OWNER), or mark one not a person (PERSON-9=KEEP).")
    ro.add_argument("matter", help="The matter folder.")
    ro.add_argument("assignments", nargs="+", metavar="PLACEHOLDER=ROLE",
                    help="e.g. PERSON-4=OWNER, PERSON-9=KEEP (not a person: leave it in), PERSON-5= (active, no role).")
    ro.set_defaults(func=cmd_roles)

    ch = sub.add_parser("check", help="Look for personal data the run left in the output (counts and ids only; "
                                      "exit 5 if anything is found).")
    ch.add_argument("matter", help="The matter folder.")
    ch.add_argument("--config", default=None, help="Config folder (default: the repo's config/).")
    ch.set_defaults(func=cmd_check)

    wh = sub.add_parser("where", help="Which names.txt line a placeholder is on now.")
    wh.add_argument("matter", help="The matter folder.")
    wh.add_argument("placeholders", nargs="+", metavar="PLACEHOLDER", help="e.g. PERSON-26 CLIENT")
    wh.set_defaults(func=cmd_where)

    ad = sub.add_parser("adopt", help="Add the word from a check finding to that person's names.txt line.")
    ad.add_argument("matter", help="The matter folder.")
    ad.add_argument("ids", nargs="+", metavar="ID", help="Finding ids from the last check, e.g. F3 F7.")
    ad.set_defaults(func=cmd_adopt)

    pr = sub.add_parser("profiles", help="List the available profiles.")
    pr.add_argument("--config", default=None)
    pr.set_defaults(func=cmd_profiles)
    return p


def main(argv: list[str] | None = None) -> int:
    netguard.install()
    # With -I, PYTHONIOENCODING is ignored and a piped console may be cp1252; a
    # path with "Ł" in it must not turn a good run into a UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SafeError as exc:
        print(f"redact-local: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("redact-local: interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Never print the message: library errors can quote document text.
        print(f"redact-local: failed ({type(exc).__name__}). The traceback is in the private run.log, if one was started.",
              file=sys.stderr)
        blocked = netguard.attempts()
        if blocked:
            _print_blocked([{"module": b["module"], "call": b["call"]} for b in blocked])
            return EXIT_NETWORK
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
