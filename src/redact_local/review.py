"""The review sheet: ``_redact/review.html``.

This file holds original values. It is for the user's eyes only and is written
to the private folder. It loads nothing from the network (no scripts, fonts or
images; a Content-Security-Policy forbids any request).
"""

from __future__ import annotations

import html
from collections import defaultdict
from datetime import datetime
from pathlib import Path

CSS = """
body{font:14px/1.45 system-ui,Segoe UI,sans-serif;margin:24px auto;max-width:1200px;padding:0 16px;color:#1d1d1f}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}
.meta{color:#555;margin-bottom:12px}.warn{background:#fff4e5;border:1px solid #f0c36d;padding:10px 12px;border-radius:6px}
.how{background:#eef5ff;border:1px solid #b6d4fe;padding:10px 14px;border-radius:6px}
table{border-collapse:collapse;width:100%;margin:6px 0 10px}th,td{border:1px solid #ddd;padding:4px 8px;text-align:left;vertical-align:top}
th{background:#f5f5f7;font-weight:600}td.n{text-align:right;white-space:nowrap}code{background:#f2f2f2;padding:0 3px;border-radius:3px}
.ph{font-family:Consolas,monospace;white-space:nowrap}.small{color:#666;font-size:12px}summary{cursor:pointer;font-weight:600}
"""


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def _where(files: set[str], limit: int = 4) -> str:
    items = sorted(files)
    shown = ", ".join(_e(f) for f in items[:limit])
    return shown + (f" <span class=small>+{len(items) - limit} more</span>" if len(items) > limit else "")


def _table(headers: list[str], rows: list[list[str]], numeric: set[int] = frozenset()) -> str:
    if not rows:
        return "<p class=small>None.</p>"
    out = ["<table><tr>" + "".join(f"<th>{_e(h)}</th>" for h in headers) + "</tr>"]
    for r in rows:
        out.append("<tr>" + "".join(f"<td class=n>{c}</td>" if i in numeric else f"<td>{c}</td>" for i, c in enumerate(r)) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


def write(path: Path, *, matter: Path, out_dir: Path, profile, files: list, skipped: list[str],
          names_info: dict, keep_count: int, totals: dict) -> None:
    redacted = defaultdict(lambda: {"count": 0, "files": set(), "detectors": set(), "category": "", "value": ""})
    kept = defaultdict(lambda: {"count": 0, "files": set(), "category": "", "reason": "", "value": ""})
    policy = defaultdict(lambda: {"count": 0, "files": set(), "category": "", "value": ""})
    keep_hits = defaultdict(lambda: {"count": 0, "files": set()})
    misses = defaultdict(lambda: {"count": 0, "files": set(), "why": "", "value": ""})

    for f in files:
        if f.analysis is None:
            continue
        label = f.source
        for a, where in [(f.analysis, label)] + [(na, f"{label} (file name)") for na in f.name_analyses]:
            for s in a.spans:
                key = (s.placeholder, a.text[s.start:s.end].casefold())
                r = redacted[key]
                r["count"] += 1
                r["files"].add(where)
                r["detectors"].add(s.detector)
                r["category"] = "names.txt" if s.category == "NAME" else s.category
                r["value"] = a.text[s.start:s.end]
            for k in a.kept:
                bucket = policy if k.reason.startswith("policy") else kept
                key = (k.value.casefold(), k.category, k.reason)
                r = bucket[key]
                r["count"] += 1
                r["files"].add(where)
                r["category"] = k.category
                r["value"] = k.value
                if bucket is kept:
                    r["reason"] = k.reason
            for _a, _b, term in a.keep_hits:
                keep_hits[term]["count"] += 1
                keep_hits[term]["files"].add(where)
            for _a, _b, value, why in a.misses:
                key = " ".join(value.split()).casefold()
                r = misses[key]
                r["count"] += 1
                r["files"].add(where)
                r["why"] = why
                r["value"] = " ".join(value.split())

    miss_rows = [
        [_e(m["value"]), str(m["count"]), _where(m["files"]), _e(m["why"])]
        for m in sorted(misses.values(), key=lambda m: (-m["count"], m["value"].casefold()))
    ]
    red_rows = [
        [f"<span class=ph>[{_e(ph)}]</span>", _e(r["value"]), _e(r["category"]), str(r["count"]),
         _e(", ".join(sorted(r["detectors"]))), _where(r["files"])]
        for (ph, _v), r in sorted(redacted.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    ]
    kept_rows = [
        [_e(r["value"]), _e(r["category"]), _e(r["reason"]), str(r["count"]), _where(r["files"])]
        for r in sorted(kept.values(), key=lambda r: (r["reason"], r["value"].casefold()))
    ]
    hit_rows = [[_e(t), str(h["count"]), _where(h["files"])] for t, h in sorted(keep_hits.items(), key=lambda kv: kv[0].casefold())]
    policy_rows = [
        [_e(r["value"]), _e(r["category"]), str(r["count"]), _where(r["files"])]
        for r in sorted(policy.values(), key=lambda r: (r["category"], r["value"].casefold()))
    ]
    file_rows = []
    for f in files:
        ex = f.extracted
        if f.error:
            detail = f"<b>Failed:</b> {_e(f.error)}"
        else:
            bits = [ex.kind]
            if ex.kind == "pdf":
                bits.append(f"{ex.pages} page(s): {ex.text_pages} text layer, {ex.ocr_pages} OCR")
                if ex.ocr_regions:
                    bits.append(f"{ex.ocr_regions} embedded image(s) OCR'd")
            detail = _e("; ".join(bits))
            if ex.notes:
                detail += "<br><span class=small>" + "<br>".join(_e(n) for n in ex.notes) + "</span>"
        file_rows.append([_e(f.source), _e(f.output or "(none)"), detail,
                          str(len(f.analysis.spans)) if f.analysis else "-"])

    names_line = (
        f"names.txt: {names_info['entries']} entr{'y' if names_info['entries'] == 1 else 'ies'}"
        + (" (template just created; fill it in and rerun)" if names_info.get("created") else "")
        + (f", {names_info['bad_lines']} line(s) not understood" if names_info.get("bad_lines") else "")
    )
    page = f"""<!doctype html>
<html lang=en><head><meta charset=utf-8>
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<meta name=robots content=noindex>
<title>redact-local review</title><style>{CSS}</style></head><body>
<h1>Redaction review</h1>
<div class=meta>Matter folder: <code>{_e(matter)}</code><br>
Output: <code>{_e(out_dir)}</code><br>
Profile: <b>{_e(profile.name)}</b> ({_e(profile.description)}) · Model: {_e(profile.spacy_model)} ·
Run: {_e(datetime.now().strftime('%Y-%m-%d %H:%M'))}<br>
{_e(names_line)} · keep-list terms loaded: {keep_count}</div>
<p class=warn><b>This page contains the original names and values.</b> It stays on this machine.
Don't share it, and don't let Claude open it.</p>

<div class=how><b>How to fix things, then rerun:</b>
<ol>
<li><b>Something real was left in</b> (see "Possible misses" and the output files): add the person, handle
or email to <code>_redact\\names.txt</code>, e.g. <code>Jane Example | @jexample =&gt; CLIENT</code>.</li>
<li><b>Something harmless was redacted</b> (a product, a court, a staff role): add it to
<code>config\\keep.txt</code> (all matters) or <code>_redact\\keep.txt</code> (this matter only).</li>
<li><b>A possible miss is just an ordinary word</b>: add it to <code>config\\common-words.txt</code> so it
stops being listed. That file doesn't change what gets redacted.</li>
<li>Rerun the same command. Placeholders stay the same across reruns.</li>
</ol>
The lists carry over, so the next document of the same kind should need few or no fixes.</div>

<h2>Totals</h2>
<p>{totals['files']} file(s) processed, {totals['skipped']} skipped, {totals['failed']} failed ·
{totals['redactions']} redaction(s), {totals['distinct']} distinct value(s) ·
{totals['misses']} possible miss(es) · {totals['blocked']} network attempt(s) blocked</p>

<h2>Possible misses ({len(miss_rows)})</h2>
<p class=small>Capitalized words and long numbers left in the output that aren't on a keep list or in
the English word list. Most are harmless. Look for people, usernames and account numbers.</p>
{_table(["Text left in", "Times", "Where", "Why listed"], miss_rows, {1})}

<h2>Redacted ({len(red_rows)})</h2>
{_table(["Placeholder", "Original", "Category", "Times", "Detected by", "Where"], red_rows, {3})}

<h2>Kept because of a keep list, citation, or title ({len(kept_rows)})</h2>
<p class=small>A detector flagged these, but a rule kept them. Check none of them is a private person.</p>
{_table(["Value", "Would have been", "Kept because", "Times", "Where"], kept_rows, {3})}

<details><summary>Keep-list terms found in the documents ({len(hit_rows)})</summary>
<p class=small>These are never redacted wherever they appear.</p>
{_table(["Term", "Times", "Where"], hit_rows, {1})}</details>

<details><summary>Kept by policy: organizations, places, dates ({len(policy_rows)})</summary>
<p class=small>The profile keeps these. Scan for people the model mistook for an organization or place.</p>
{_table(["Value", "Type", "Times", "Where"], policy_rows, {2})}</details>

<h2>Files</h2>
{_table(["Source", "Output (redacted name)", "How it was read", "Redactions"], file_rows, {3})}
{"<h2>Skipped (unsupported type)</h2>" + _table(["File"], [[_e(s)] for s in skipped]) if skipped else ""}
</body></html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
