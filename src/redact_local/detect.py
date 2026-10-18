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
    if category in {"EMAIL", "IBAN", "IP", "TICKET", "DRIVER-LICENSE"}:
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
        self._titles = sorted(profile.official_titles, key=len, reverse=True)
        self._title_re = (
            re.compile(r"(?:" + "|".join(re.escape(t) for t in self._titles) + r")\s*$", re.I)
            if self._titles else None
        )

    # ── public ──

    def on_keep_list(self, value: str) -> bool:
        """True if ``value`` is exactly a keep-list term (ignoring case unless the term is all caps)."""
        return normalize_term(value) in self._keep_ci or " ".join(value.replace(".", "").split()) in self._keep_cs

    def analyze(self, text: str) -> Analysis:
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

        self._filter(text, candidates, result)
        self._misses(text, docs, result)
        for s in result.spans:
            s.value = text[s.start:s.end]
        return result

    # ── internals ──

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
            # A person name partly covered by something stronger: keep the rest.
            for a, b in _subtract((s.start, s.end), clashes):
                a, b = _trim(text, a, b)
                if b - a >= 2 and re.search(r"[^\W\d_]{2}", text[a:b]):
                    final.append(Span(a, b, s.category, s.detector, ident_for("PERSON", text[a:b]), priority=s.priority))
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
