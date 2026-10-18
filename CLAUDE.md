# redact-local: rules for Claude in this repo

This tool exists because reading a file in a Claude Code session sends its contents to a
hosted model. There is no safe peek. So: **never read, print, sample or search a record
about an identifiable person** while working on this repo.

## What that means here

- **Test only with synthetic data generated in code** (`tests/`). Use obviously fake names
  (Quentin Fakerton, Marisol Testwell). Never copy a real document into this repo, not even
  "to reproduce a bug". Build a fake document with the same shape instead.
- **Never open a matter folder's `_redact/` subfolder.** `names.txt`, `keep.txt`, `key.csv`,
  `review.html`, `manifest.json` and `run.log` all hold real values.
- **Never open the input documents.** Run the CLI and report its counts.
- **Never open `* - restored.*` files** or `_redact/restored/`. They hold the real names.
- **Read a `* - redacted` folder only after the user says they've checked `review.html`.**
- **The CLI must never print document text, detected values, or the contents of names.txt,
  key.csv or review.html.** Counts and paths only. Exception messages must not echo document
  text either: raise `redact_local.errors.SafeError` with a fixed message, and send details to
  the private `_redact/run.log`. `tests/test_cli.py` checks this; keep it passing.
- **Don't run a recursive search from a directory that holds data.** Scope searches to `src/`,
  `tests/`, `config/`, `skill/` and `scripts/`.
- **No enforcement hook.** The skill is a working practice, not an enforced control, and the
  docs say so. Don't describe it as more than that.

## Layout

- `src/redact_local/`: `cli` (entry point), `netguard` (in-process network block, installed
  before heavy imports), `extract` (PDF/image/text to text), `detect` (Presidio + spaCy + custom
  regexes), `patterns`, `citations`, `names` (names.txt), `placeholders` (key.csv), `review`
  (review.html), `restore`, `pipeline` (ties it together).
- `config/`: `keep.txt`, `keep-court-records.txt`, `profiles.toml`, `common-words.txt`.
- `skill/SKILL.template.md`: the skill. `scripts/install_skill.py` renders `skill/SKILL.md`
  (gitignored, holds local paths) and links it into `~/.claude/skills/redact-local`.

## Running tests

```
<venv>/Scripts/python -m pytest -q
```

Keep the venv outside any synced folder; the spaCy model is about 400 MB.
