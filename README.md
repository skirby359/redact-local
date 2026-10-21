# redact-local

[![tests](https://github.com/skirby359/redact-local/actions/workflows/tests.yml/badge.svg)](https://github.com/skirby359/redact-local/actions/workflows/tests.yml)

De-identify a folder of documents **on this machine**, before Claude reads any of it.

Reading a file in a Claude Code session sends its contents to a hosted model. There is
no upload step and no safe peek. So when a matter folder holds records about other
people (screenshots, a PDF, emails), the documents have to be de-identified *locally*
first. This project is a Python CLI that does that, plus a Claude Code skill that tells
Claude how to use it without ever opening the originals.

Everything runs locally: [Presidio](https://github.com/data-privacy-stack/presidio)
(analyzer + anonymizer) with spaCy's `en_core_web_lg` model, PyMuPDF for PDFs, and
RapidOCR (bundled ONNX models) for images and scanned pages. The default keep lists and
citation patterns are tuned for Washington State legal work; the rest is general.

> **Redaction is never perfect. You are responsible for checking the output.** The tool
> finds most names and identifiers, lists what it isn't sure about, and makes fixing
> misses quick, but it will miss things: unusual names, garbled OCR, lowercase
> usernames. Review `review.html` and the output before you rely on it or share it. This
> is software, not legal advice, and it comes with no warranty (see [LICENSE](LICENSE)).

> **The skill is a working practice, not an enforced control.** Nothing technically stops
> a Claude session from opening the original documents. The skill's rules are what keep
> that from happening. An enforcement hook was considered and deliberately left out: it
> would get in the way when working with public records.

## Installing

Developed on Windows 11 with Python 3.13; the test suite also passes on Linux with
Python 3.11 (CI runs both). macOS should work but hasn't been tested.

Install from a clone, in editable mode. The keep lists and profiles in `config/` are meant
to be edited, and the tool reads them from the clone.

```
git clone https://github.com/skirby359/redact-local
cd redact-local
python -m venv <venv>          # put it outside any synced folder (Dropbox, OneDrive): the model is ~400 MB
<venv>/Scripts/python -m pip install -e ".[docx]"         # <venv>/bin/python on macOS/Linux
<venv>/Scripts/python -m pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.8.0/en_core_web_lg-3.8.0-py3-none-any.whl
<venv>/Scripts/python scripts/install_skill.py            # optional: the Claude Code skill
```

`install_skill.py` writes `skill/SKILL.md` from the template with your venv's Python path
and links it into `~/.claude/skills/redact-local` (a junction on Windows, a symlink
elsewhere; `--copy` to copy instead). Start a new Claude Code session and `/redact-local`
is available.

Don't install Presidio's cloud extras (azure-ai-language, ahds, langextract,
transformers, gliner); this tool doesn't use them and they talk to remote services.

Below, `redact-local` means `<venv>/Scripts/python -I -m redact_local` (or the
`redact-local` command if the venv is active). `-I` keeps Python from importing anything
from the current folder.

## The workflow

```
write names.txt → run → check review.html → fix the lists → rerun
               → Claude reads the "- redacted" folder → Claude drafts with placeholders → restore
```

1. **Write `<matter>\_redact\names.txt`** with the people you want removed (format below).
   If you skip this, the first run creates a template and tells you.
2. **Run** (or ask Claude to, via the skill):
   ```
   redact-local run "C:\path\to\matter"
   ```
   It prints counts and two paths, nothing else.
3. **Open `<matter>\_redact\review.html` yourself.** It lists every redacted value,
   everything kept and why, and "possible misses".
4. **Fix the lists**: misses go in `names.txt`; false positives go in `config\keep.txt`
   (all matters), `config\keep.local.txt` (all matters, never committed) or
   `<matter>\_redact\keep.txt` (this matter). Ordinary words that keep showing up as
   possible misses go in `config\common-words.txt`.
5. **Rerun.** Placeholders don't change between runs, so nothing you've already read goes
   stale. The lists carry over, so the next document of the same kind should need few or
   no fixes. That's the point: correcting is a one-time job per kind of document.
6. **Tell Claude you've reviewed it.** Claude then reads `<matter> - redacted\`.
7. **Claude drafts using the placeholders** (`[CLIENT]`, `[PERSON-2]`...), then you (or
   Claude) run `restore` to get the real-name version, which Claude doesn't open:
   ```
   redact-local restore "draft.md" --matter "C:\path\to\matter"
   ```

## Folders

For a matter folder `X`:

| Path | What | Who reads it |
|---|---|---|
| `X\` | your originals (never modified) | you |
| `X\_redact\names.txt` | people to remove (you write it) | you |
| `X\_redact\keep.txt` | optional per-matter keep list | you |
| `X\_redact\key.csv` | placeholder → original value (persistent) | you, and `restore` |
| `X\_redact\review.html` | the review sheet (contains originals) | you |
| `X\_redact\manifest.json`, `run.log` | bookkeeping; the log can hold file names and tracebacks | you |
| `X - redacted\` | one `.txt` per source file, plus `_summary.json` (counts only) | Claude, after you've reviewed |

Output file names are redacted too (`Jane_Example notice.pdf` → `[CLIENT] notice.pdf.txt`),
including subfolder names. Only text is written: images are never copied.

A file or subfolder is treated as part of its matter, which is the nearest folder at or
above it that has a `_redact` folder: after a first `run "X"`, `run "X\emails"` or
`run "X\notice.pdf"` uses `X\_redact` and writes into `X - redacted\`, leaving the
outputs for the rest of the matter alone. The CLI prints the matter folder it used.

Before detection the text is normalized so variants can't slip past: Unicode NFKC
(ligatures, fullwidth letters), soft hyphens and zero-width characters removed, curly
quotes straightened, `\r\n` made `\n`, and words hyphenated across a line break rejoined.
So the output text can differ from the source in those small ways.

**Tip:** the matter folder's own path is visible to Claude (it's in the command). Name
matter folders without personal names where you can (`2026-014 account notice`, not
`Jane Example`).

## names.txt

```
# One person per line. Aliases separated by |. Optional role after =>.
Jane Example | Janie | @jexample | jane@example.com => CLIENT
Pat Staffer => STAFF
Sam Staffer => STAFF
Lee Witness
```

- With a role: `[CLIENT]`, and their email/handle/phone become `[CLIENT-EMAIL]`,
  `[CLIENT-HANDLE]`, `[CLIENT-PHONE]`. Two lines with the same role are numbered:
  `[STAFF-1]`, `[STAFF-2]`. Numbers stick to the person, so adding someone later never
  renumbers anyone. Without a role: the next `[PERSON-n]`. A role must start with a
  letter and can't be a built-in category (`PERSON`, `EMAIL`, `ADDRESS`...); the run
  stops and names the line if it is.
- Matching ignores case. For `Jane Q. Example` it also matches `Jane Example`,
  `Example, Jane`, `J. Example`, `jane_example`, `JaneExample`, `Example2`, `the
  Examples`, and `Jane` / `Example` on their own. Which variants are generated is
  `name_variants` in `config/profiles.toml`.
- Suffixes (`Jr.`, `Sr.`, `III`, `Esq.`) are ignored when building variants, an entry can
  be written `Example, Jane`, and each half of a hyphenated surname (`Example-Smith`)
  also matches on its own.
- A one-word variant that is an ordinary English word (`Grant`, `Will`) only matches
  when capitalized. One that is on the keep list (`Washington`) is skipped.
- names.txt wins over the keep list. In the `court-records` profile, a legal citation
  wins over names.txt: `State v. Example, 123 Wn.2d 456` stays as written because
  published case names are public record. The prose mentions are still redacted.
- `restore` puts back the first alias on the line.

## What gets detected

**Redacted by default** (the `redact` list in `config/profiles.toml`):

| Category | How |
|---|---|
| PERSON | spaCy NER via Presidio, plus names.txt |
| EMAIL, PHONE | Presidio (plus a US phone regex as a backstop) |
| HANDLE | `@handles`; usernames in profile URLs (`github.com/<user>`, `x.com/<user>`, `linkedin.com/in/<user>`...) become `github.com/[HANDLE-1]`. Site pages like `github.com/settings` are left alone. |
| SSN, DRIVER-LICENSE, CARD, IBAN, IP | Presidio (plus Washington's `WDL…` licence format) |
| ADDRESS | custom regex for US street addresses and PO boxes (Presidio has none). City and state stay. |
| DOB | custom: a date next to "DOB", "date of birth", "born"... |
| TICKET | custom: numbers after ticket / case / account / order / reference..., and `#1234567` |

**Kept by default:** organizations, city/state-level places, ordinary dates, legal
citations (reporter and statute parts always: `123 Wn.2d 456`, `RCW 9A.04.110`, WAC,
U.S.C., C.F.R., court rules), and anything on a keep list. The citation patterns are
adapted from [wa-cite-check](https://github.com/skirby359/wa-cite-check).

Kept orgs and places still appear under "Possible misses" when they aren't on a keep
list, because a person mislabeled as a place is the classic miss.

## Profiles

| Profile | What it does |
|---|---|
| `client` (default) | Strict. Every person not on a keep list is redacted, including names inside citations. |
| `court-records` | For public court records. Also keeps case names in citations, court case numbers, WSBA numbers, names right after a judicial title (Judge, Justice, Hon., Commissioner...) and names directly followed by a WSBA number (counsel). Add judges and counsel you've decided may stay to `config/keep-court-records.txt`. Contact details and IDs are still removed. |

**Choosing a looser profile is your policy call, not the tool's.** And **voter data stays
strict** even though it's public record: never run voter files through `court-records`.

## The review sheet

`review.html` (local only, no network requests, contains originals) has:

- **Possible misses**: capitalized words and long numbers left in the output that
  aren't on a keep list or in the English word list (spaCy's lemmatizer tables, about
  83,000 words, plus `config/common-words.txt`). Most are harmless; look for people,
  usernames and account numbers.
- **Redacted**: every distinct value with placeholder, category, count, detector and files.
- **Kept**: values a detector flagged but a keep list, citation or title rule kept.
- Keep-list terms found, and orgs/places/dates kept by policy (collapsed).
- **Files**: how each was read (text layer, OCR pages, embedded images, attached files)
  and any failures.

### Check key.csv for look-alike variants

After each run, open `<matter>\_redact\key.csv` yourself (Claude must not) and scan
each category for near-duplicates. These are one person, address or username split across
several placeholders because a character or two differs. For example,
`jexample0x@example.me`, `jexampleOx@example.me` and `jexampleox@example.me` would come out
as three separate `[EMAIL-n]`s. The usual causes:

- **OCR confusions** in screenshots and scanned pages: `0`/`O`/`o`, `1`/`l`/`I`,
  `rn`/`m`, `5`/`S`, a dropped or extra `.`. When the source is an image, there's no way
  to recover which character was really there. Ask for the original (an `.eml`, or a
  text PDF) if it matters.
- **Characters from another alphabet**: Cyrillic `о а е х с р` look identical to Latin
  `o a e x c p` but are different characters, so they don't match. Case, Unicode
  compatibility forms and zero-width characters are already folded; look-alike letters
  aren't.
- **Real aliases**: one mailbox under several domains (Proton's `@proton.me`,
  `@protonmail.com` and `@pm.me`, for example).

**Why it matters:**
- Anyone reading the redacted text, Claude included, will take three placeholders as
  three different people or addresses and may reason from that.
- `restore` puts each variant back exactly as captured, so a misread version goes back
  into your draft. Proofread restored copies.

**Fix:** list every variant on the person's line in `names.txt` and rerun. They become
`[CLIENT-EMAIL]`, `[CLIENT-EMAIL-2]`, `[CLIENT-EMAIL-3]`..., so it's clear they belong to
one person. Put the correct spelling first and draft with that placeholder.

```
Jane Example | jexample0x@example.me | jexampleOx@example.me | jexampleox@example.me => CLIENT
```

## Restore

`restore <draft> --matter <folder>` swaps placeholders for the primary values in
`key.csv` and writes `<draft> - restored.<ext>` next to the draft. Supports `.txt`, `.md`
and `.docx` (formatting is kept unless a placeholder was split across Word runs, in which
case that paragraph's runs are merged). Placeholders not in the key are left as they are.
If the draft is inside a `- redacted` folder, the restored copy goes to
`<matter>\_redact\restored\` instead, so real names never land where Claude reads.

## The network guard

Neither Presidio nor spaCy documents any telemetry, but the tool doesn't rely on that.
Before any heavy import it:

- sets `TLDEXTRACT_PUBLIC_SUFFIX_LIST_URLS=""` (tldextract, used by Presidio's email
  recognizer, otherwise fetches the Public Suffix List on first use; with no URLs it uses
  its bundled snapshot), `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `PRESIDIO_DEVICE=cpu`;
- replaces `socket.socket.connect` / `connect_ex` / `sendto`, `socket.create_connection`,
  `getaddrinfo` and the `gethostby*` lookups with a function that raises
  `NetworkBlockedError` naming the module that tried, and records the attempt.

If anything tries, the run exits with code 3 and names the module, even if the library
caught the error. Presidio would otherwise download a missing spaCy model on the fly; the
tool checks for the model first and stops with an install hint instead.

**Limits:** this is in-process only. It covers Python code in this process that goes
through `socket`. It does not cover native code opening sockets on its own, or child
processes. It's a tripwire, not a firewall. For a hard guarantee, add an outbound
firewall rule blocking your venv's `python` executable (that also blocks `pip` from the
venv, so lift it while updating packages).

## Known gaps

- spaCy misses some names, especially single unusual names, names in ALL CAPS, and names
  in UI text with no sentence around them. names.txt and the possible-misses list exist
  for this.
- Lowercase usernames on their own (`jexample` with no `@` or profile URL) aren't
  detected unless they're in names.txt.
- OCR quality depends on the image. Low-contrast or tiny text can come out garbled, and a
  garbled name may slip past detection (it will usually show up as a possible miss).
- OCR misreads and look-alike letters from other alphabets split one identity into several
  placeholders. They aren't merged automatically. See
  [Check key.csv for look-alike variants](#check-keycsv-for-look-alike-variants).
- EML attachments are named, not opened: save them into the matter folder to include them.
  Files attached to a PDF (a PDF Portfolio's documents, whose only page is usually a cover
  sheet, and paperclip attachments on a page) *are* read if they're a supported type. The
  rest are counted as "not opened"; one that can't be read makes the run partial (exit 4).
- PDF comments (sticky notes, comments on highlights) aren't extracted. Text boxes drawn on
  the page are.
- DOCX: footnotes, comments and text boxes aren't extracted.
- English only.

## Tests

```
<venv>/Scripts/python -m pip install -e ".[docx,test]"
<venv>/Scripts/python -m pytest -q
```

All fixtures are synthetic and generated in code (Quentin Fakerton, Marisol Testwell...).
The suite covers names.txt roles and variants, the keep list, citations in both profiles,
placeholder stability across files and reruns, redacted file names, OCR on a rendered PNG,
PDF text layer and scanned-page fallback, files attached to PDFs (portfolios, paperclips,
nesting, unreadable attachments), EML and DOCX, restore round trips (text and DOCX), the
network guard, and a check that the CLI's stdout and stderr never contain any fake name or
value, including when a file fails.

## Layout

```
src/redact_local/
  cli.py          entry point; routes library output to the private run.log
  netguard.py     in-process network block
  extract.py      PDF / image / text / EML / DOCX → text
  detect.py       Presidio + spaCy + names.txt + regexes; keep list; citations; misses
  patterns.py     custom regexes (handles, profile URLs, addresses, DOB, tickets)
  citations.py    citation spans (adapted from wa-cite-check)
  names.py        names.txt parsing and matching
  placeholders.py key.csv
  review.py       review.html
  restore.py      restore command
  pipeline.py     ties a run together
config/           keep.txt, keep-court-records.txt, profiles.toml, common-words.txt
skill/            SKILL.template.md (SKILL.md is generated by scripts/install_skill.py)
scripts/          install_skill.py
tests/            pytest, synthetic fixtures only
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: **never attach real documents
or paste real names into an issue.** Build a synthetic reproduction.

## License

MIT, see [LICENSE](LICENSE).

The dependencies are under their own licenses. Most are permissive (Presidio, spaCy and
`en_core_web_lg`, python-docx and ONNX Runtime are MIT; RapidOCR and its bundled PP-OCR
models are Apache-2.0; Pillow is MIT-CMU). The exception is **PyMuPDF, which is
AGPL-3.0** (or a commercial license from Artifex). This repository doesn't include
PyMuPDF; pip installs it separately. If you redistribute a bundle that includes PyMuPDF
(a frozen executable, a container image, a hosted service), the AGPL applies to that
bundle.
