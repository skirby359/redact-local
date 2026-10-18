# Contributing

Thanks for helping. One rule comes before everything else.

## Never share real documents or real names

This tool exists to keep personal data off servers you don't control. A GitHub issue,
pull request, discussion or gist is public and permanent. So:

- **Don't attach real documents, screenshots, `review.html`, `key.csv`, `names.txt`,
  `run.log`, or redacted output** that came from real documents.
- **Don't paste real names, emails, handles or account numbers**, even "just one".
- **Build a synthetic reproduction instead.** Make a fake document with the same shape
  (same layout, same kind of name or number, obviously fake values like "Quentin
  Fakerton" or "555-0142"), check that it reproduces the problem, and share that. The
  test suite's `tests/conftest.py` has helpers for rendering fake screenshots and PDFs.

If you think you've found a bug that **leaks** data (a detected value surviving into the
output, or anything document-derived printed to the console), please report it privately
through GitHub's "Report a vulnerability" (Security tab) rather than a public issue.

## Development

```
git clone https://github.com/skirby359/redact-local
cd redact-local
python -m venv .venv            # or anywhere outside a synced folder; the model is ~400 MB
.venv/Scripts/python -m pip install -e ".[docx,test]"   # .venv/bin/python on macOS/Linux
.venv/Scripts/python -m pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.8.0/en_core_web_lg-3.8.0-py3-none-any.whl
.venv/Scripts/python -m pytest -q
```

Tests must use synthetic data generated in code. The console-leak test
(`tests/test_cli.py`) must keep passing: the CLI may print counts and paths, never
document text, detected values, or file names from inside a matter folder. Raise
`redact_local.errors.SafeError` with fixed wording for user-facing errors; anything else
is reported by exception type only.
