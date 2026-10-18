"""Command line for redact-local.

    redact-local run <path> [--profile client|court-records]
    redact-local restore <draft> --matter <folder>
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
import sys  # noqa: E402
import traceback  # noqa: E402
from pathlib import Path  # noqa: E402

from redact_local import __version__  # noqa: E402
from redact_local.errors import SafeError  # noqa: E402

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_NETWORK, EXIT_PARTIAL = 0, 1, 2, 3, 4


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
            result = pipeline.run(args.path, args.profile, args.config)
        except SafeError:
            raise
        except BaseException:
            _log_traceback(log_path)
            raise

    s = result.summary
    f, pg, r, k = s["files"], s["pages"], s["redactions"], s["kept"]
    types = ", ".join(f"{t} {n}" for t, n in f["by_type"].items()) or "none"
    print(f"redact-local {__version__} | profile: {result.profile} | model: {result.model}")
    print(f"Files: {f['processed']} processed ({types}); {f['skipped']} skipped (unsupported type); {f['failed']} failed")
    read = []
    if pg["pdf_pages"]:
        read.append(f"PDF pages {pg['pdf_pages']} ({pg['pdf_text_layer']} text layer, {pg['pdf_ocr']} OCR)")
    if pg["embedded_images_ocr"]:
        read.append(f"{_plural(pg['embedded_images_ocr'], 'embedded image')} OCR")
    if pg["images_ocr"]:
        read.append(f"{_plural(pg['images_ocr'], 'image file')} OCR")
    if read:
        print("Read: " + "; ".join(read))
    cats = ", ".join(f"{c} {n}" for c, n in sorted(r["by_category"].items(), key=lambda kv: -kv[1]))
    print(f"Redactions: {r['total']} ({r['distinct_values']} distinct)" + (f": {cats}" if cats else ""))
    print(f"Kept: {k['keep_list_occurrences']} keep-list occurrence(s), {k['kept_by_rule']} by rule, "
          f"{k['protected_citations']} protected citation span(s)")
    print(f"Possible misses to check: {s['possible_misses']}")
    if result.names_created:
        print(f"names.txt: none yet, so a template was created at {result.private / 'names.txt'}")
        print("  Add the people to remove, then rerun.")
    if f["failed"]:
        print(f"{_plural(f['failed'], 'file')} could not be read; see the Files table in the review sheet.")
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

    if result.blocked:
        _print_blocked(result.blocked)
        return EXIT_NETWORK
    return EXIT_PARTIAL if f["failed"] or s.get("stale_outputs_not_removed") else EXIT_OK


def cmd_restore(args) -> int:
    from redact_local import restore

    key = restore.find_key(Path(args.matter))
    log_path = key.parent / "run.log"
    with _quiet(log_path):
        try:
            result = restore.restore(args.draft, args.matter)
        except SafeError:
            raise
        except BaseException:
            _log_traceback(log_path)
            raise
    print(f"Restored {_plural(result.replaced, 'placeholder')} ({result.distinct} distinct); "
          f"{result.unknown} not in the key, left as they were.")
    print(f"Output: {result.output}")
    print("  It contains the real values. Open it yourself; Claude must not read it.")
    return EXIT_OK


def cmd_profiles(args) -> int:
    from redact_local.config import config_dir, list_profiles

    for name, desc in list_profiles(config_dir(args.config)).items():
        print(f"{name}: {desc}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="redact-local", description="De-identify matter documents on this machine.")
    p.add_argument("--version", action="version", version=f"redact-local {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="Redact a matter folder (or one file in it).")
    r.add_argument("path", help="Matter folder, or a single file inside one.")
    r.add_argument("--profile", default="client", help="Profile from config/profiles.toml (default: client).")
    r.add_argument("--config", default=None, help="Config folder (default: the repo's config/).")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("restore", help="Swap placeholders in a draft back to the real values.")
    s.add_argument("draft", help="Draft written with placeholders (.txt, .md or .docx).")
    s.add_argument("--matter", required=True, help="The matter folder the placeholders came from.")
    s.set_defaults(func=cmd_restore)

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
