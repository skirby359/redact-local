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
   contains it. **The one exception is the output folder, `<folder>\redacted\`** (rule 4).
   Go to it by its full path, as the CLI prints it; never list the input folder to find
   it, and keep any search inside it.
2. **Run the CLI and report only what it prints**: counts and paths.
3. **Tell the user to open `review.html` themselves** and check it. Never open it.
4. **Read `<folder>\redacted\` only after the user confirms they've reviewed it.**
   Reading `_summary.json` there is fine at any time (counts only). Use the "Output
   folder:" path the CLI printed. Matters run before 0.2.0 have an older copy beside the
   folder, `<folder> - redacted\`. When the CLI reports one, tell the user they can delete
   it, and don't read it: it's no longer updated.
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
"{{PYTHON}}" -I -m redact_local run "<folder>" --suggest-names
"{{PYTHON}}" -I -m redact_local roles "<folder>" PERSON-4=OWNER PERSON-2=SHOP PERSON-9=KEEP
"{{PYTHON}}" -I -m redact_local restore "<draft.md|.txt|.docx>" --matter "<folder>"
"{{PYTHON}}" -I -m redact_local profiles
```

Exit codes: 0 ok · 1 error · 2 bad arguments · 3 a network attempt was blocked (treat
the run as failed and tell the user) · 4 some files, or files attached to a PDF, couldn't
be read or written (the rest were written; the review sheet's Files table says which), or
an earlier output couldn't be removed. **If the CLI prints a WARNING about an earlier output, tell the user and don't
read the output folder until a rerun no longer shows it.** That file may carry an older,
less-redacted name.

Running on a subfolder or a single file uses the matter it belongs to (the CLI prints
which), so `run` the matter folder itself the first time. If the matter already has a
`redacted` subfolder of the user's own, the run stops with exit 1 and asks them to rename
it.

The CLI's first line, the top of `review.html`, `_summary.json` ("build") and the first
line of each output file show the tool version, as `0.2.0 (b62ff95)`: the version, then
the git commit. `+changes` after the commit means the run used uncommitted code. Use it
when the user asks which version produced a run.

Profiles: `client` (default, strict: every person not on a keep list is redacted) and
`court-records` (keeps judges, counsel and citations; still removes contact details and
IDs). Choosing the looser profile is the user's call. Ask; don't pick it yourself.

## Workflow

1. **Run** the CLI on the matter folder. Relay the counts and the paths it prints. The user
   doesn't need to write `names.txt` first: if there isn't one, the run creates
   `<folder>\_redact\names.txt` listing the people it found, **commented out**, and
   prints how many. While `names.txt` is still the untouched blank template (a matter
   started before this feature), a run fills in the suggestions the same way.
   `--suggest-names` adds people found later to a `names.txt` the user has already
   edited (only ones not on it yet; the user's lines are never changed).
2. **The user edits `names.txt`**: deletes the `# ` in front of each real person, adds a
   role after `=>` if they want one (`Jane Example => CLIENT`, so the output says
   `[CLIENT]` instead of `[PERSON-3]`), and leaves the rest commented out. A suggestion
   that isn't a person (a part name, a line of work notes) gets `=> KEEP`: the next run
   leaves it in the output. One person per line:
   `Jane Example | @jexample | jane@example.com => CLIENT`.
3. **The user opens `review.html`** and fixes the lists:
   - something real left in → add it to `_redact\names.txt`
   - something harmless redacted → add it to `{{CONFIG_DIR}}/keep.txt`
     (all matters) or `_redact/keep.txt` (this matter)
   - a "possible miss" that's just a word → `{{CONFIG_DIR}}/common-words.txt`
4. **Rerun** until the user is satisfied. Placeholders stay stable across reruns.
5. **The user tells you it's reviewed.** Then read `<folder>\redacted\`.
   - **Roles from context, without seeing names:** if people are still `[PERSON-n]`,
     propose roles from what the redacted text shows (`PERSON-4 → OWNER: signs the
     repair order as customer`). The user checks each against `review.html`. On their OK,
     run `roles` with the mapping, then rerun, then reread. One person under several
     placeholders (several `names.txt` lines): join them with `+`
     (`PERSON-1+PERSON-3+PERSON-5=MECHANIC`) and `roles` merges their lines into one.
     `roles` finds each line by the placeholder it produces now (then its note, then the
     key), changes all or nothing, keeps a `names.txt.bak`, and prints counts and line
     numbers only.
   - Something redacted that plainly isn't a person (a part name, `[PERSON-7] 6 hrs`):
     propose `PERSON-7=KEEP` the same way. It will appear in the output after the rerun,
     so the user decides.
6. **Draft** with placeholders. Save the draft outside the input folder.
7. **Restore**: run `restore`, relay the counts and output path, and tell the user the
   restored file is theirs to open.
