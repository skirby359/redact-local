---
name: redact-local
description: De-identify a folder or file of documents ON THIS MACHINE before Claude reads any of it. Use when the user asks to redact, de-identify or anonymize documents, or when you are about to read documents about third parties (clients, opposing parties, platform staff, voters). Runs a local CLI (Presidio + spaCy + local OCR) and reports counts only; Claude reads the redacted output only after the user has reviewed it.
---

<!-- Generated from SKILL.template.md by scripts/install_skill.py. Edit the template, then rerun the script. -->

# redact-local

Reading a file in Claude Code sends its contents to a hosted model. There is no safe
peek. This skill keeps third-party documents on the user's machine until they've been
de-identified and the user has checked the result.

**This is a working practice, not an enforced control.** Nothing technically stops
Claude from opening the originals. These rules are what keep that from happening.
Follow them exactly.

## Rules for Claude

1. **Never open, read, list, search, grep or print anything inside the input folder or
   its `_redact` subfolder.** That includes `names.txt`, `keep.txt`, `key.csv`,
   `review.html`, `manifest.json` and `run.log`. Don't `ls` the input folder either:
   file names can contain names. Don't run a recursive search from any folder that
   contains it.
2. **Run the CLI and report only what it prints**: counts and paths.
3. **Tell the user to open `review.html` themselves** and check it. Never open it.
4. **Read the `<folder> - redacted` folder only after the user confirms they've reviewed
   it.** Reading `_summary.json` there is fine at any time (counts only).
5. **Write drafts with the placeholders** (`[CLIENT]`, `[PERSON-2]`, `[EMAIL-1]`...).
   Use `restore` to make the real-name version, and **never open the restored file**
   (`* - restored.*`, or anything in `_redact\restored\`).
6. **If the user starts typing real names into chat** to add to the list, suggest they
   edit `<folder>\_redact\names.txt` directly instead, so the names stay off the model.
7. If a task can't be done without seeing the originals, stop and say so.
8. Voter data stays on the strict `client` profile, even though it's public record.

## Commands

The CLI runs from its own venv, with `-I` so nothing in the current folder is imported:

```
"{{PYTHON}}" -I -m redact_local run "<folder or file>"
"{{PYTHON}}" -I -m redact_local run "<folder>" --profile court-records
"{{PYTHON}}" -I -m redact_local restore "<draft.md|.txt|.docx>" --matter "<folder>"
"{{PYTHON}}" -I -m redact_local profiles
```

Exit codes: 0 ok · 1 error · 2 bad arguments · 3 a network attempt was blocked (treat
the run as failed and tell the user) · 4 some files couldn't be read or written (the rest
were written; the review sheet's Files table says which), or an earlier output couldn't
be removed. **If the CLI prints a WARNING about an earlier output, tell the user and don't
read the output folder until a rerun no longer shows it.** That file may carry an older,
less-redacted name.

Running on a subfolder or a single file uses the matter it belongs to (the CLI prints
which), so `run` the matter folder itself the first time.

Profiles: `client` (default, strict: every person not on a keep list is redacted) and
`court-records` (keeps judges, counsel and citations; still removes contact details and
IDs). Choosing the looser profile is the user's call. Ask; don't pick it yourself.

## Workflow

1. **Before the first run**, ask the user to write `<folder>\_redact\names.txt` (if the
   run finds none it creates a template and says so). One person per line:
   `Jane Example | @jexample | jane@example.com => CLIENT`. Roles are optional.
2. **Run** the CLI. Relay the counts and the two paths it prints.
3. **The user opens `review.html`** and fixes the lists:
   - something real left in → add it to `_redact\names.txt`
   - something harmless redacted → add it to `{{CONFIG_DIR}}/keep.txt`
     (all matters) or `_redact/keep.txt` (this matter)
   - a "possible miss" that's just a word → `{{CONFIG_DIR}}/common-words.txt`
4. **Rerun** until the user is satisfied. Placeholders stay stable across reruns.
5. **The user tells you it's reviewed.** Then read `<folder> - redacted\`.
6. **Draft** with placeholders. Save the draft outside the input folder.
7. **Restore**: run `restore`, relay the counts and output path, and tell the user the
   restored file is theirs to open.
