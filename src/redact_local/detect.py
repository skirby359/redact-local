"""Find what to redact in a piece of text.

Sources, from highest priority to lowest when two overlap:

1. names.txt emails, handles and phone numbers
2. structured identifiers: Presidio (email, phone, SSN, driver licence, card,
   IBAN, IP) and our regexes (handles, profile URLs, addresses, DOB, tickets)
3. names.txt names and their variants
4. person names from spaCy (via Presidio)

Then, for everything except names.txt matches:
- anything inside a keep-list term is dropped (and reported as kept);
- anything inside a protected citation or case number is dropped;
- in court-records, a name right after "Judge"/"Hon."... or on a WSBA line is kept.
Inside a protected citation, even names.txt matches are left alone, because
the profile says citations are never redacted.

Organizations, places, dates and nationalities are found too but kept unless
the profile's ``redact`` list includes them. They are listed in review.html.
"""

from __future__ import annotations

from redact_local import netguard

netguard.install()

import logging  # noqa: E402
import re  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402

from redact_local import citations, names, patterns  # noqa: E402
from redact_local.config import (  # noqa: E402
    Profile, flexible_term_body, flexible_term_regex, normalize_term, term_is_case_sensitive,
)
from redact_local.errors import SafeError  # noqa: E402

for _name in ("presidio-analyzer", "presidio-anonymizer", "spacy", "decision_process"):
    logging.getLogger(_name).setLevel(logging.ERROR)

PRESIDIO_ENTITIES = {
    "PERSON": "PERSON",
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "US_SSN": "SSN",
    "US_DRIVER_LICENSE": "DRIVER-LICENSE",
    "CREDIT_CARD": "CARD",
    "IBAN_CODE": "IBAN",
    "IP_ADDRESS": "IP",
    "LOCATION": "LOCATION",
    "ORGANIZATION": "ORG",
    "DATE_TIME": "DATE",
    "NRP": "NRP",
}
POLICY_CATEGORIES = {"LOCATION", "ORG", "DATE", "NRP"}
# Categories a keep-list term can shield (emails and phones are added where it's
# used). Government and payment IDs are removed even inside a keep-list term.
# What a citation shields is decided by _shields().
SHIELDABLE = {"PERSON", "NAME", "ADDRESS", "TICKET", "DRIVER-LICENSE", "HANDLE", *POLICY_CATEGORIES}

P_NAMES_STRUCT, P_STRUCT, P_NAMES, P_PERSON, P_POLICY = 100, 80, 70, 50, 10

_NER_MAPPING = {
    "PERSON": "PERSON", "PER": "PERSON", "NORP": "NRP", "FAC": "LOCATION", "LOC": "LOCATION",
    "GPE": "LOCATION", "ORG": "ORGANIZATION", "DATE": "DATE_TIME", "TIME": "DATE_TIME",
}
_CHUNK = 100_000
# "Jane's", "Mary-Kate's": a capitalized word with a possessive 's.
_POSSESSIVE = re.compile(r"(?<![^\W\d_])([A-Z][^\W\d_]+(?:-[A-Z][^\W\d_]+)?)['’]s(?![^\W\d_])")
_IRREGULAR_PLURALS = {"children": "child", "women": "woman", "men": "man", "people": "person"}
_OCR_MARK = re.compile(r"^\[OCR\]\n", re.M)
_WORD = re.compile(r"[^\W\d_]+")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")

# First names that are also ordinary English words. The model often reads them as the word
# ("Will" the verb, "Hope" the noun), and "Possible misses" skips ordinary words, so a lone
# "Will" could be left in without a flag. These get position rules (_word_name_position).
# Left out on purpose: months and weekdays, and names that here usually mean a place or a
# company (Chase, Pierce, Mason, King).
WORD_NAMES = frozenset("""
    amber art autumn barb belle bill bob bonnie brook bud buddy candy carol carter charity chip
    chuck clay cliff colt daisy dale dawn dean destiny don drew duke earl ed eve faith fern flora
    frank gale gene ginger glen grace grant guy hank harmony hazel heather herb holly hope hunter
    iris ivy jack jade jasmine jay jean joe joy lark laurel lily mark max melody mercy miles misty
    nan nick olive opal pat patience pearl peg penny piper randy raven ray reed rex rich rob robin
    rod rose rowan ruby rusty ruth sage sally sandy sue summer sunny ted terry tom victor violet
    wade warren will willow woody wren
""".split())
_WORD_NAME_RE = re.compile(r"(?<![^\W\d_])[A-Z][a-z]+(?![^\W\d_])")
# Words that make an email display name a business, not a person ("Example Auto Repair").
_BUSINESS_WORDS = frozenset("""
    inc llc pllc lllp llp ltd lp co corp corporation company group services service support team
    notifications notification noreply reply news newsletter digital media law legal office offices
    department dept auto automotive repair brake brakes bank credit union store shop customer care
    sales billing info admin help desk center centre clinic hospital school university college
    county city state court clerk agency the of and
""".split())
# The word, not the name: "the Bill", "a Grant Deed", "Last Will and Testament".
_NOT_NAME_BEFORE = {
    "the", "a", "an", "this", "that", "these", "those", "any", "each", "every", "no", "some",
    "our", "your", "his", "her", "their", "its", "my", "last",
}
_NOT_NAME_AFTER = re.compile(
    r"[ \t]*(?:\d|of\b|(?:County|City|Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Way|Boulevard|Blvd"
    r"|Court|Ct|Park|River|Creek|Lake|Mountain|Valley|Bay|Island|Bank|Hall|School|College|University"
    r"|Hospital|Center|Centre|Deed|Garden|Gardens|Program|Fund|Act|Agreement|Foundation|Group|Inc|LLC"
    r"|Corp|Company)\b)"
)
_SPEECH_AFTER = re.compile(
    r"[ \t]+(?:said|says|wrote|writes|called|calls|asked|asks|told|tells|replied|replies|emailed|texted"
    r"|mentioned|agreed|confirmed|explained|noted)\b"
)
_LINE_START = re.compile(r"^[ \t>]*(?:(?:[-*•–]|\d+[.)])[ \t]*)?$")
_CLOSING = re.compile(
    r"^[ \t>]*(?:thanks|thank you|many thanks|best|best regards|regards|kind regards|warm regards"
    r"|sincerely|yours(?: truly| sincerely)?|cheers|cordially|respectfully|take care|all the best)[ \t,.!]*$",
    re.I,
)


def _word_name_position(text: str, a: int, b: int) -> bool | None:
    """Whether the capitalized word at text[a:b] sits where a name goes.

    True: after a greeting or header label ("Hi Will,", "From: Will"), mid-sentence
    ("Bill and Will said"), at a sentence start when a comma or a "said"-type verb follows
    ("Will, please...", "Hope wrote..."), or alone on the line after a closing ("Best,\\nHope").
    False: the ordinary word ("Will you...", "Hope this helps", "the Bill of Sale",
    "Grant County"). None: alone on a line with no closing above, so it can't be told.
    """
    line_start = text.rfind("\n", 0, a) + 1
    line_end = text.find("\n", b)
    line_end = len(text) if line_end < 0 else line_end
    before, after = text[line_start:a], text[b:line_end]
    if _LINE_START.match(before) and not after.strip(" \t.,!"):
        prev = next((ln for ln in reversed(text[:line_start].split("\n")) if ln.strip(" \t>")), "")
        return True if _CLOSING.match(prev) else None
    if _NOT_NAME_AFTER.match(after):
        return False
    prev_word = re.search(r"([^\W\d_]+)[^\w\n]*$", before)
    if prev_word and prev_word.group(1).lower() in _NOT_NAME_BEFORE:
        return False
    stripped = before.rstrip(" \t\"'“‘(")
    if _LINE_START.match(before) or stripped.endswith((".", "!", "?")):
        return after.startswith(",") or bool(_SPEECH_AFTER.match(after))
    return True


def ocr_ranges(text: str) -> list[tuple[int, int]]:
    """Where the OCR'd text is: from each "[OCR]" line to the next page break or the end."""
    out = []
    for m in _OCR_MARK.finditer(text):
        nxt = text.find("\n--- page ", m.end())
        out.append((m.end(), len(text) if nxt < 0 else nxt))
    return out


def _edit_distance(a: str, b: str, limit: int) -> int:
    """Levenshtein distance with adjacent transpositions, stopping early past ``limit``."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if prev2 is not None and i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]
_WSBA = r"WSBA\s*(?:No\.?|#|Number)?\s*:?\s*\d{3,6}\b"
_WSBA_AFTER = re.compile(rf"[ \t]*,?[ \t]*(?:\(\s*)?{_WSBA}")
_WSBA_NEXT_LINE = re.compile(rf"[^\n]{{0,40}}\n[ \t]*{_WSBA}")


@dataclass
class Span:
    start: int
    end: int
    category: str          # PERSON, EMAIL, ..., or NAME for names.txt
    detector: str
    ident: str             # identity used to pick a placeholder
    fixed: str | None = None    # names.txt placeholder (without brackets), e.g. CLIENT-EMAIL
    primary: str | None = None  # value restore should put back (names.txt canonical)
    priority: int = 0
    value: str = ""
    placeholder: str = ""       # set by the pipeline, without brackets
    kind: str = ""              # names.txt alias kind: name | email | handle | phone


@dataclass
class Kept:
    start: int
    end: int
    value: str
    category: str
    reason: str
    detector: str


@dataclass
class Analysis:
    text: str
    spans: list[Span] = field(default_factory=list)
    kept: list[Kept] = field(default_factory=list)
    keep_hits: list[tuple[int, int, str]] = field(default_factory=list)
    protected: list[tuple[int, int, str]] = field(default_factory=list)
    misses: list[tuple[int, int, str, str]] = field(default_factory=list)


# ── Identity keys: two mentions with the same key share a placeholder ──────

def ident_for(category: str, value: str) -> str:
    v = value.strip()
    if category in {"PHONE", "SSN", "CARD"}:
        digits = re.sub(r"\D", "", v)
        if category == "PHONE" and len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        return f"{category}|{digits}"
    if category == "HANDLE":
        return f"HANDLE|{v.lstrip('@＠').casefold()}"
    if category in {"EMAIL", "IBAN", "IP", "TICKET", "DRIVER-LICENSE", "VIN", "PLATE"}:
        return category + "|" + re.sub(r"\s+", "", v).casefold()
    # Names and free text: ignore case, punctuation and spacing.
    return category + "|" + " ".join(re.findall(r"[^\W_]+", v.casefold()))


# ── Engine construction (spaCy load is cached per process) ─────────────────

_ANALYZERS: dict[str, object] = {}
_ENGLISH: dict[str, set[str]] = {}


def _analyzer(model: str):
    if model in _ANALYZERS:
        return _ANALYZERS[model]
    import spacy

    if not spacy.util.is_package(model):
        # Presidio would try to download it; the network guard would stop that.
        raise SafeError(
            f"spaCy model '{model}' is not installed in this venv. "
            f"See README.md, 'Installing', to install it once."
        )
    from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_analyzer.predefined_recognizers import (
        CreditCardRecognizer, EmailRecognizer, IbanRecognizer, IpRecognizer, PhoneRecognizer,
        SpacyRecognizer, UsLicenseRecognizer, UsSsnRecognizer,
    )

    nlp_engine = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": model}],
        "ner_model_configuration": {
            "model_to_presidio_entity_mapping": _NER_MAPPING,
            "low_confidence_score_multiplier": 0.4,
            "low_score_entity_names": [],
            "labels_to_ignore": ["CARDINAL", "EVENT", "LANGUAGE", "LAW", "MONEY", "ORDINAL",
                                 "PERCENT", "PRODUCT", "QUANTITY", "WORK_OF_ART"],
        },
    }).create_engine()
    registry = RecognizerRegistry(
        recognizers=[
            SpacyRecognizer(supported_entities=["PERSON", "LOCATION", "ORGANIZATION", "DATE_TIME", "NRP"]),
            EmailRecognizer(), PhoneRecognizer(), UsSsnRecognizer(), UsLicenseRecognizer(),
            CreditCardRecognizer(), IbanRecognizer(), IpRecognizer(),
        ],
        supported_languages=["en"],
    )
    analyzer = AnalyzerEngine(nlp_engine=nlp_engine, registry=registry, supported_languages=["en"])
    _ANALYZERS[model] = analyzer
    return analyzer


def english_words(analyzer, model: str) -> set[str]:
    """spaCy's English word list (WordNet lemmas from the lemmatizer tables) plus stop words."""
    if model in _ENGLISH:
        return _ENGLISH[model]
    from spacy.lang.en.stop_words import STOP_WORDS

    words = set(STOP_WORDS)
    nlp = analyzer.nlp_engine.nlp["en"]
    if "lemmatizer" in nlp.pipe_names:
        lookups = nlp.get_pipe("lemmatizer").lookups
        if lookups.has_table("lemma_index"):
            for wordlist in lookups.get_table("lemma_index").values():
                words.update(w.lower() for w in wordlist)
    _ENGLISH[model] = words
    return words


def _overlaps(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 < b1 and b0 < a1


def _inside(a0: int, a1: int, spans) -> tuple | None:
    for s in spans:
        if s[0] <= a0 and a1 <= s[1]:
            return s
    return None


def _shields(kind: str, s: Span) -> bool:
    """Whether a protected span of this kind leaves this detection alone."""
    if kind == "citation (case name)":
        # Published case names are public record (court-records profile only).
        return s.category in {"PERSON", *POLICY_CATEGORIES} or (s.category == "NAME" and s.kind == "name")
    if kind == "citation":
        # Reporter/statute numbers: never shields a person, handle or contact detail.
        return s.category in {"ADDRESS", "TICKET", "DRIVER-LICENSE", "PHONE", *POLICY_CATEGORIES}
    if kind == "case or bar number":
        return s.category in {"TICKET", "DRIVER-LICENSE", "PHONE", *POLICY_CATEGORIES}
    return False


def _chunks(text: str) -> list[tuple[int, str]]:
    """Split long text so spaCy stays under its length limit.

    Cuts at a paragraph break, else a line break, else a space, so a token (an
    email, a name) is never split between chunks.
    """
    if len(text) <= _CHUNK:
        return [(0, text)]
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + _CHUNK)
        if end < len(text):
            for sep in ("\n\n", "\n", " ", "\t"):
                cut = text.rfind(sep, start + _CHUNK // 2, end)
                if cut > start:
                    end = cut + len(sep)
                    break
        out.append((start, text[start:end]))
        start = end
    return out


class Engine:
    """One engine per run: profile, keep list, names.txt and the loaded model."""

    def __init__(self, profile: Profile, keep_terms: list[str], common_words: set[str], names_file: names.NamesFile):
        self.profile = profile
        self.analyzer = _analyzer(profile.spacy_model)
        self.english = english_words(self.analyzer, profile.spacy_model)
        self.common = common_words
        self.keep_terms = keep_terms
        # Exact-term lookups. All-caps terms compare case-sensitively ("US" is not "us").
        self._keep_ci = {normalize_term(t) for t in keep_terms if not term_is_case_sensitive(t)}
        self._keep_cs = {" ".join(t.replace(".", "").split()) for t in keep_terms if term_is_case_sensitive(t)}
        self._keep_res = [(t, re.compile(flexible_term_regex(t), re.I)) for t in keep_terms]
        # Presidio's allow list, anchored so a keep term never hides a longer
        # value that merely contains it (an email at github.com, say).
        self.allow_list = [
            rf"\A\W*(?{'-i' if term_is_case_sensitive(t) else ''}:{flexible_term_body(t)})\W*\Z" for t in keep_terms
        ]
        self.names_file = names_file
        self.matchers = names.build_matchers(
            names_file, profile.name_variants, self.on_keep_list, self.english | common_words
        )
        # Name words from names.txt (4+ letters) to look for misspelled in OCR'd text.
        self._fuzzy: list[tuple[str, names.NameEntry]] = []
        for entry in names_file.entries:
            for alias in entry.aliases:
                if alias.kind != "name":
                    continue
                for tok in names._name_tokens(alias.text):
                    t = tok.strip(".").lower()
                    if len(t) >= 4 and t.isalpha():
                        self._fuzzy.append((t, entry))
        self._titles = sorted(profile.official_titles, key=len, reverse=True)
        self._title_re = (
            re.compile(r"(?:" + "|".join(re.escape(t) for t in self._titles) + r")\s*$", re.I)
            if self._titles else None
        )

    # ── public ──

    def on_keep_list(self, value: str) -> bool:
        """True if ``value`` is exactly a keep-list term (ignoring case unless the term is all caps)."""
        return normalize_term(value) in self._keep_ci or " ".join(value.replace(".", "").split()) in self._keep_cs

    def analyze(self, text: str, is_name: bool = False) -> Analysis:
        """``is_name``: the text is a file or folder name, not a sentence."""
        result = Analysis(text=text)
        candidates: list[Span] = []
        docs = []

        # names.txt
        for m in self.matchers:
            for hit in m.regex.finditer(text):
                if hit.end() <= hit.start():
                    continue
                entry = m.entry
                if m.kind == "name":
                    cat, prio = "NAME", P_NAMES
                    fixed = entry.placeholder_base
                    ident = f"NAMES|{fixed}" if fixed else ident_for("PERSON", entry.canonical)
                    if not fixed:
                        cat = "PERSON"
                    primary = entry.canonical
                else:
                    kind_cat = {"email": "EMAIL", "handle": "HANDLE", "phone": "PHONE"}[m.kind]
                    prio = P_NAMES_STRUCT
                    if entry.placeholder_base:
                        cat = "NAME"
                        suffix = kind_cat if m.index == 1 else f"{kind_cat}-{m.index}"
                        fixed = f"{entry.placeholder_base}-{suffix}"
                        ident = f"NAMES|{fixed}"
                        primary = m.alias
                    else:
                        cat, fixed, primary = kind_cat, None, None
                        ident = ident_for(kind_cat, hit.group(0))
                candidates.append(Span(hit.start(), hit.end(), cat, f"names.txt ({m.variant})", ident,
                                       fixed=fixed, primary=primary, priority=prio, kind=m.kind))

        # Presidio + spaCy, chunked
        for offset, chunk in _chunks(text):
            artifacts = self.analyzer.nlp_engine.process_text(chunk, "en")
            docs.append((offset, artifacts.tokens))
            found = self.analyzer.analyze(
                chunk, language="en", entities=list(PRESIDIO_ENTITIES),
                score_threshold=self.profile.score_threshold, allow_list=self.allow_list,
                allow_list_match="regex", nlp_artifacts=artifacts,
            )
            for r in found:
                cat = PRESIDIO_ENTITIES[r.entity_type]
                start, end = offset + r.start, offset + r.end
                start, end = _trim(text, start, end)
                if end <= start:
                    continue
                name = (r.recognition_metadata or {}).get("recognizer_name", "Presidio")
                detector = "spaCy NER" if name == "SpacyRecognizer" else f"Presidio {name}"
                prio = P_PERSON if cat == "PERSON" else (P_POLICY if cat in POLICY_CATEGORIES else P_STRUCT)
                candidates.append(Span(start, end, cat, detector, ident_for(cat, text[start:end]), priority=prio))

        # Our regexes
        for detector in patterns.ALL:
            for start, end, cat, name in detector(text):
                candidates.append(Span(start, end, cat, name, ident_for(cat, text[start:end]), priority=P_STRUCT))

        # A display name in an email header ('From: "Example, Jane (J.)" <jane@x.com>'), whole:
        # the model often takes only one half of a "Last, First" name.
        for start, end, raw in patterns.header_display_names(text):
            if self._display_name_is_person(raw):
                candidates.append(Span(start, end, "PERSON", "email header name", ident_for("PERSON", raw),
                                       priority=P_PERSON))

        if self.profile.redact_possessive_names:
            candidates.extend(self._possessive_names(text, candidates, is_name))
        candidates.extend(self._word_names(text))

        # OCR'd text: labelled form fields, and names.txt names the OCR misspelled.
        ocr = ocr_ranges(text)
        for start, end, cat, name in patterns.form_fields(text, ocr):
            prio = P_PERSON if cat == "PERSON" else P_STRUCT
            candidates.append(Span(start, end, cat, name, ident_for(cat, text[start:end]), priority=prio))
        if ocr and self._fuzzy:
            candidates.extend(self._ocr_misspellings(text, ocr))

        self._filter(text, candidates, result)
        self._misses(text, docs, result)
        for s in result.spans:
            s.value = text[s.start:s.end]
        return result

    # ── internals ──

    def _display_name_is_person(self, raw: str) -> bool:
        """Two to five name-like words, not all ordinary words, and none that marks a business."""
        toks = [t for t in re.findall(r"[^\W\d_]+", raw) if len(t) > 1]
        if not 2 <= len(toks) <= 5 or any(t.lower() in _BUSINESS_WORDS for t in toks):
            return False
        if self.on_keep_list(raw) or any(self.on_keep_list(t) for t in toks):
            return False
        return not all(self._ordinary(t) for t in toks)

    def _ordinary(self, word: str) -> bool:
        low = word.lower()
        forms = {low, _IRREGULAR_PLURALS.get(low, low)}
        if low.endswith("s"):
            forms.add(low[:-1])
        return any(f in self.english or f in self.common for f in forms)

    def _ocr_misspellings(self, text: str, ocr: list[tuple[int, int]]) -> list[Span]:
        """names.txt names as OCR misreads them: "Qventin", "Exampel", "QuentinExample", "Exam ple".

        Only inside OCR'd text. Words are also split where OCR ran them together
        ("toJaneExampel" -> "Jane", "Exampel"), and two pieces split by one space are
        tried joined. What counts as a match is ``_ocr_name_match``. Keep-list terms
        never match. A match takes the person's placeholder.
        """
        out = []
        for start, end in ocr:
            pieces, cands = [], []
            for m in _WORD.finditer(text, start, end):
                word, off, prev = m.group(0), m.start(), 0
                cuts = [c.start() for c in _CAMEL.finditer(word)]
                if cuts:
                    cands.append((off, off + len(word), word, False))   # the whole run too: "GrAnt"
                for cut in cuts + [len(word)]:
                    if cut > prev:
                        pieces.append((off + prev, off + cut, word[prev:cut], bool(cuts)))
                    prev = cut
            cands += pieces
            pairs = [(a1, b2, w1 + w2) for (a1, b1, w1, _), (a2, b2, w2, _) in zip(pieces, pieces[1:])
                     if text[b1:a2] == " "]
            for a, b, w, glued in cands:
                entry = self._ocr_name_match(w, glued)
                if entry is not None:
                    fixed = entry.placeholder_base
                    ident = f"NAMES|{fixed}" if fixed else ident_for("PERSON", entry.canonical)
                    out.append(Span(a, b, "NAME" if fixed else "PERSON", "names.txt (OCR misspelling)", ident,
                                    fixed=fixed, primary=entry.canonical, priority=P_NAMES, kind="name"))
            # One name split by a space ("Exam ple"): the joined pieces, edit distance only.
            for a, b, w in pairs:
                entry = self._ocr_name_match(w, False, whole_word_only=True)
                if entry is not None:
                    fixed = entry.placeholder_base
                    ident = f"NAMES|{fixed}" if fixed else ident_for("PERSON", entry.canonical)
                    out.append(Span(a, b, "NAME" if fixed else "PERSON", "names.txt (OCR misspelling)", ident,
                                    fixed=fixed, primary=entry.canonical, priority=P_NAMES, kind="name"))
        return out

    def _ocr_name_match(self, w: str, glued: bool, whole_word_only: bool = False):
        """The names.txt entry an OCR'd word is a misreading of, or None.

        - A word that isn't an ordinary word: one edit from a name word (two for 7+
          letters; 5 letters or fewer must keep the first letter), or starting with one
          when the OCR ran it into more letters ("Grntstilowed").
        - An ordinary word ("grant", "bill", "will") only when it is exactly the name and
          the OCR shows it: run into the next word ("grantPed") or oddly capitalized ("GrAnt").
          A plain "bill" on an invoice stays.
        """
        low = w.lower()
        if len(low) < 3 or self.on_keep_list(w):
            return None
        ordinary = self._ordinary(low)
        odd_case = not (w.islower() or w.isupper() or w.istitle())
        for tok, entry in self._fuzzy:
            if low == tok:
                if not ordinary or glued or odd_case:
                    return entry
                continue
            if ordinary or (len(tok) <= 5 and low[0] != tok[0]):
                continue
            limit = 2 if len(tok) >= 7 else 1
            if _edit_distance(low, tok, limit) <= limit:
                return entry
            if not whole_word_only and len(low) > len(tok) + 1 and low[0] == tok[0]:
                if any(_edit_distance(low[:n], tok, 1) <= 1 for n in (len(tok) - 1, len(tok), len(tok) + 1)):
                    return entry
        return None

    def _possessive_names(self, text: str, candidates: list[Span], is_name: bool) -> list[Span]:
        """The person in "Quentin's Truck Repair", which the model keeps as an organization.

        In text, only inside an ORG or LOCATION detection: the model already judged the rest.
        In a file or folder name, any capitalized possessive, since there's no sentence to go on.
        Ordinary words ("Driver's", "Children's") and keep-list terms are left alone; the
        filter that follows applies the keep list and citations as for any other name.
        """
        ranges = [(0, len(text))] if is_name else [
            (c.start, c.end) for c in candidates if c.category in {"ORG", "LOCATION"}]
        out, seen = [], set()
        for start, end in ranges:
            for m in _POSSESSIVE.finditer(text, start, min(len(text), end + 2)):  # the 's may sit past the span
                a, b = m.span(1)
                word = m.group(1)
                if a < start or (a, b) in seen or self._ordinary(word) or self.on_keep_list(word):
                    continue
                seen.add((a, b))
                out.append(Span(a, b, "PERSON", "possessive in a name", ident_for("PERSON", word), priority=P_PERSON))
        return out

    def _word_names(self, text: str) -> list[Span]:
        """First names that are also ordinary words, where they sit in a name position.

        Treated like any model detection afterwards: the keep list, citations and
        names.txt still win.
        """
        out = []
        for m in _WORD_NAME_RE.finditer(text):
            word = m.group(0)
            if word.lower() in WORD_NAMES and _word_name_position(text, m.start(), m.end()):
                out.append(Span(m.start(), m.end(), "PERSON", "first name that is also a word",
                                ident_for("PERSON", word), priority=P_PERSON))
        return out

    def _protected_spans(self, text: str) -> list[tuple[int, int, str]]:
        name_spans, number_spans = citations.citation_spans(text)
        out = [(a, b, "citation") for a, b in number_spans]
        if self.profile.protect_citation_names:
            out += [(a, b, "citation (case name)") for a, b in name_spans]
        if self.profile.protect_case_numbers:
            out += [(a, b, "case or bar number") for a, b in citations.case_number_spans(text)]
        return out

    def _official_reason(self, text: str, s: Span) -> str | None:
        if not self.profile.keep_titled_officials or s.category != "PERSON":
            return None
        before = text[max(0, s.start - 40):s.start]
        if self._title_re and self._title_re.search(before):
            return "titled official"
        # Counsel: "Jane Example, WSBA No. 12345" or a signature block whose
        # next line starts with the WSBA number.
        after = text[s.end:s.end + 60]
        if _WSBA_AFTER.match(after) or _WSBA_NEXT_LINE.match(after):
            return "counsel (WSBA number)"
        return None

    def _filter(self, text: str, candidates: list[Span], result: Analysis) -> None:
        keep_hits = []
        for term, rx in self._keep_res:
            keep_hits.extend((m.start(), m.end(), term) for m in rx.finditer(text))
        result.keep_hits = keep_hits
        protected = self._protected_spans(text)
        result.protected = protected
        redact = self.profile.redact

        survivors: list[Span] = []
        for s in candidates:
            value = text[s.start:s.end]
            from_names = s.detector.startswith("names.txt")
            shield = next((p for p in protected if p[0] <= s.start and s.end <= p[1] and _shields(p[2], s)), None)
            if shield:
                result.kept.append(Kept(s.start, s.end, value, s.category, f"protected {shield[2]}", s.detector))
                continue
            if not from_names:
                hit = _inside(s.start, s.end, keep_hits)
                if hit and s.category in SHIELDABLE | {"EMAIL", "PHONE"}:
                    result.kept.append(Kept(s.start, s.end, value, s.category, f"keep list: {hit[2]}", s.detector))
                    continue
                if s.category == "PERSON" and self.on_keep_list(value):
                    result.kept.append(Kept(s.start, s.end, value, s.category, "keep list", s.detector))
                    continue
                reason = self._official_reason(text, s)
                if reason:
                    result.kept.append(Kept(s.start, s.end, value, s.category, reason, s.detector))
                    continue
                if s.category not in redact:
                    result.kept.append(Kept(s.start, s.end, value, s.category, "policy (not redacted by this profile)", s.detector))
                    continue
            survivors.append(s)

        # Resolve overlaps: higher priority first, then longer.
        survivors.sort(key=lambda s: (-s.priority, -(s.end - s.start), s.start))
        claimed: list[tuple[int, int]] = []
        final: list[Span] = []
        for s in survivors:
            clashes = [c for c in claimed if _overlaps(s.start, s.end, *c)]
            if not clashes:
                final.append(s)
                claimed.append((s.start, s.end))
                continue
            if s.category != "PERSON":
                continue  # structured values are all-or-nothing
            # A person name partly covered by something stronger: redact the rest, unless
            # the rest is a keep-list term ("Qty Description" swept in after a name), the
            # same rule a whole detection gets above.
            for a, b in _subtract((s.start, s.end), clashes):
                a, b = _trim(text, a, b)
                if b - a >= 2 and re.search(r"[^\W\d_]{2}", text[a:b]):
                    piece = text[a:b]
                    hit = _inside(a, b, keep_hits)
                    if hit or self.on_keep_list(piece):
                        result.kept.append(Kept(a, b, piece, s.category,
                                                f"keep list: {hit[2]}" if hit else "keep list", s.detector))
                        continue
                    final.append(Span(a, b, s.category, s.detector, ident_for("PERSON", piece), priority=s.priority))
                    claimed.append((a, b))
        final.sort(key=lambda s: s.start)
        result.spans = final
        # A place/date the profile keeps but that sits inside something we did
        # redact (a date of birth, an address) was not actually kept.
        result.kept = [
            k for k in result.kept
            if not (k.reason.startswith("policy") and any(_overlaps(k.start, k.end, s.start, s.end) for s in final))
        ]

    def _misses(self, text: str, docs, result: Analysis) -> None:
        """Capitalized words left in the output that no list explains."""
        # Organizations and places the profile keeps are NOT treated as explained:
        # a person the model mislabeled as a place is exactly what to catch here.
        looks_named = [(k.start, k.end, k.category) for k in result.kept
                       if k.reason.startswith("policy") and k.category in {"LOCATION", "ORG", "NRP"}]
        covered = [(s.start, s.end) for s in result.spans]
        covered += [(k.start, k.end) for k in result.kept
                    if not (k.reason.startswith("policy") and k.category in {"LOCATION", "ORG", "NRP"})]
        covered += [(a, b) for a, b, _ in result.keep_hits]
        covered += [(a, b) for a, b, _ in result.protected]

        def is_covered(a: int, b: int) -> bool:
            return any(_overlaps(a, b, c0, c1) for c0, c1 in covered)

        def reason(a: int, b: int, propn: bool) -> str:
            for c0, c1, cat in looks_named:
                if _overlaps(a, b, c0, c1):
                    kind = {"LOCATION": "a place", "ORG": "an organization", "NRP": "a group"}[cat]
                    return f"kept: the model called it {kind}; check it isn't a person"
            return "proper noun not on any list" if propn else "not in the English word list"

        misses: list[tuple[int, int, str, str]] = []
        for offset, doc in docs:
            run: list = []
            for tok in list(doc) + [None]:
                flag = False
                if tok is not None:
                    a, b = offset + tok.idx, offset + tok.idx + len(tok.text)
                    word = tok.text
                    lower = word.lower()
                    if (
                        len(word) >= 2 and word[0].isupper() and any(c.isalpha() for c in word)
                        and not tok.like_url and not tok.like_email and not is_covered(a, b)
                        and lower not in self.common and not self.on_keep_list(word)
                        and not (tok.pos_ != "PROPN" and (lower in self.english or tok.lemma_.lower() in self.english))
                    ):
                        flag = True
                if flag:
                    run.append(tok)
                    continue
                if run:
                    a = offset + run[0].idx
                    b = offset + run[-1].idx + len(run[-1].text)
                    misses.append((a, b, text[a:b], reason(a, b, any(t.pos_ == "PROPN" for t in run))))
                    run = []
        # A name-like ordinary word alone on a line ("Grace" in a contact list) is not
        # redacted, since it can't be told from a heading, but it is listed.
        for m in _WORD_NAME_RE.finditer(text):
            a, b = m.span()
            if (m.group(0).lower() in WORD_NAMES and _word_name_position(text, a, b) is None
                    and not is_covered(a, b) and not self.on_keep_list(m.group(0))
                    and not any(_overlaps(a, b, x[0], x[1]) for x in misses)):
                misses.append((a, b, m.group(0), "a first name that is also an ordinary word, alone on a line"))
        # spaCy often calls a bare number a DATE, so policy-kept spans don't explain numbers.
        numbers_covered = [(s.start, s.end) for s in result.spans]
        numbers_covered += [(k.start, k.end) for k in result.kept if not k.reason.startswith("policy")]
        numbers_covered += [(a, b) for a, b, _ in (*result.keep_hits, *result.protected)]
        for a, b in patterns.long_numbers(text):
            if not any(_overlaps(a, b, c0, c1) for c0, c1 in numbers_covered) and not re.fullmatch(r"\d{4}[-/ ]\d{1,2}[-/ ]\d{1,2}|\d{1,2}[-/ ]\d{1,2}[-/ ]\d{4}", text[a:b]):
                misses.append((a, b, text[a:b], "long number left in"))
        result.misses = misses


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and not text[start].isalnum() and text[start] not in "@＠+(":
        start += 1
    while end > start and not text[end - 1].isalnum():
        end -= 1
    return start, end


def _subtract(span: tuple[int, int], cuts: list[tuple[int, int]]) -> list[tuple[int, int]]:
    pieces = [span]
    for c0, c1 in cuts:
        nxt = []
        for a, b in pieces:
            if c1 <= a or b <= c0:
                nxt.append((a, b))
                continue
            if a < c0:
                nxt.append((a, c0))
            if c1 < b:
                nxt.append((c1, b))
        pieces = nxt
    return pieces
