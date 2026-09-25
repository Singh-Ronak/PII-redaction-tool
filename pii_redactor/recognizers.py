"""Custom Presidio recognizers for PII that the stock registry misses or that
needs India-specific handling (phone formats, Aadhaar, DIN, addresses...).

Recognizers whose regexes depend on letter case (company suffixes, name
labels, address anchors) subclass ``EntityRecognizer`` directly, because
``AnalyzerEngine.analyze`` applies ``re.IGNORECASE`` to pattern recognizers.

Pattern recognizers with a low base score rely on Presidio's context enhancer
(surrounding words or the ``context`` argument passed to ``analyze``) to cross
their threshold. That keeps bare 8-digit or 12-digit numbers from being
redacted unless a label such as "DIN" or "Aadhaar" is nearby.
"""

from __future__ import annotations

import re
from typing import Iterable

from presidio_analyzer import EntityRecognizer, Pattern, PatternRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpArtifacts

from .config import INDIAN_STATES

_MONTHS = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"

# --------------------------------------------------------------------------- #
# Verhoeff checksum (used by Aadhaar numbers)
# --------------------------------------------------------------------------- #
_V_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_V_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def verhoeff_valid(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    check = 0
    for i, d in enumerate(reversed(digits)):
        check = _V_D[check][_V_P[i % 8][d]]
    return check == 0


class IndianPhoneRecognizer(PatternRecognizer):
    """Indian landline/mobile/toll-free formats seen in offer documents."""

    PATTERNS = [
        Pattern("IN phone +91", r"(?<![\w+])\+\s?91[\s-]*(?:\(\s?\d{2,4}\s?\)[\s-]*)?\d(?:[\s-]?\d){7,10}(?![\d-])", 0.7),
        Pattern("IN phone 91-", r"(?<![\d+])91-\d{2,4}-\d{3,4}\s?\d{4}(?!\d)", 0.6),
        Pattern("IN landline STD", r"(?<![\w-])0\d{2,4}[\s-]\d{6,8}(?![\d-])", 0.35),
        Pattern("IN mobile", r"(?<![\w.,/])[6-9]\d{9}(?![\w.,/])", 0.35),
        Pattern("IN mobile split", r"(?<![\d.,])[6-9]\d{4}\s\d{5}(?![\d.,])", 0.35),
        Pattern("IN toll free", r"(?<!\d)1800[\s-]?\d{3}[\s-]?\d{3,4}(?!\d)", 0.35),
    ]
    CONTEXT = ["phone", "telephone", "tel", "mobile", "contact", "fax", "call", "ph", "helpline", "cell"]

    def __init__(self):
        super().__init__(
            supported_entity="PHONE_NUMBER", name="IndianPhoneRecognizer",
            patterns=self.PATTERNS, context=self.CONTEXT, supported_language="en",
        )


class AadhaarRecognizer(PatternRecognizer):
    """12-digit Aadhaar numbers. Needs context; a valid Verhoeff checksum adds confidence."""

    PATTERNS = [Pattern("Aadhaar", r"(?<![\d,.])[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}(?![\d,.])", 0.2)]
    CONTEXT = ["aadhaar", "aadhar", "uid", "uidai", "आधार", "unique", "identification", "enrolment", "vid"]

    def __init__(self):
        super().__init__(
            supported_entity="IN_AADHAAR", name="AadhaarRecognizer",
            patterns=self.PATTERNS, context=self.CONTEXT, supported_language="en",
        )

    def analyze(self, text, entities, nlp_artifacts=None, regex_flags=None):
        results = super().analyze(text, entities, nlp_artifacts, regex_flags)
        for r in results:
            if verhoeff_valid(text[r.start:r.end]):
                r.score = min(1.0, r.score + 0.15)
        return results


class DinRecognizer(PatternRecognizer):
    """Director Identification Number: a personal 8-digit identifier (context required)."""

    def __init__(self):
        super().__init__(
            supported_entity="IN_DIN", name="DinRecognizer",
            patterns=[Pattern("DIN", r"(?<![\d,.])\d{8}(?![\d,.])", 0.05)],
            context=["din", "director identification"], supported_language="en",
        )


class DateOfBirthRecognizer(PatternRecognizer):
    """Dates are only PII when they are birth dates.

    In-text cues are checked here, more strictly than Presidio's
    window-based context enhancer: the cue ("DOB", "born on", "जन्म") must be
    in the same sentence *before* the date, with no other date in between, so
    "DOB: 14/07/1986. Account opened 03/02/2019" only flags the first date.
    Such results are marked as already context-scored so the enhancer leaves
    them alone. A date in a text without any cue keeps its low base score and
    can still be boosted by external context (e.g. a "Date of Birth" table
    column header passed to ``analyze(context=...)``).
    """

    PATTERNS = [
        Pattern("DOB numeric", r"(?<!\d)\d{1,2}[/.-]\d{1,2}[/.-](?:19|20)\d{2}(?!\d)", 0.05),
        Pattern("DOB iso", r"(?<!\d)(?:19|20)\d{2}-\d{2}-\d{2}(?!\d)", 0.05),
        Pattern("DOB d month y", rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTHS}\.?,?\s+(?:19|20)\d{{2}}\b", 0.05),
        Pattern("DOB month d y", rf"\b{_MONTHS}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+(?:19|20)\d{{2}}\b", 0.05),
    ]
    CONTEXT = ["dob", "birth", "born", "d.o.b", "जन्म", "birthday"]
    CUE_RE = re.compile(r"(?i)\b(?:d\.?o\.?b\.?|date\s+of\s+birth|birth\s*date|birthday|born(?:\s+on|\s+in)?)(?!\w)|जन्म")
    SENTENCE_END = re.compile(r"(?<!\b[A-Za-z])[.;!?]\s|\n")  # "D.O.B. 11/08/1994" is not a sentence end
    CUE_SCORE = 0.85

    def __init__(self):
        super().__init__(
            supported_entity="DATE_OF_BIRTH", name="DateOfBirthRecognizer",
            patterns=self.PATTERNS, context=self.CONTEXT, supported_language="en",
        )

    def analyze(self, text, entities, nlp_artifacts=None, regex_flags=None):
        results = super().analyze(text, entities, nlp_artifacts, regex_flags)
        if not self.CUE_RE.search(text):
            return results
        date_spans = [(r.start, r.end) for r in results]
        for r in results:
            sentence_start = max((m.end() for m in self.SENTENCE_END.finditer(text, 0, r.start)), default=0)
            cues = list(self.CUE_RE.finditer(text, sentence_start, r.start))
            if cues and not any(cues[-1].end() <= s < r.start for s, _ in date_spans):
                r.score = self.CUE_SCORE
            r.recognition_metadata[RecognizerResult.IS_SCORE_ENHANCED_BY_CONTEXT_KEY] = True
        return results


class BrokenUrlRecognizer(PatternRecognizer):
    """Web addresses split by a stray space in PDF-to-Word conversions ("www.example. com")."""

    def __init__(self):
        super().__init__(
            supported_entity="URL", name="BrokenUrlRecognizer", supported_language="en",
            patterns=[Pattern("www split TLD", r"(?<![\w.])www\.[\w-]+(?:\.[\w-]+)*\.\s(?:com|in|org|net|co\.in)\b", 0.6)],
        )


def _result(entity: str, start: int, end: int, score: float, recognizer: EntityRecognizer) -> RecognizerResult:
    return RecognizerResult(
        entity_type=entity, start=start, end=end, score=score,
        recognition_metadata={
            RecognizerResult.RECOGNIZER_NAME_KEY: recognizer.name,
            RecognizerResult.RECOGNIZER_IDENTIFIER_KEY: recognizer.id,
        },
    )


class OrganizationSuffixRecognizer(EntityRecognizer):
    """Company names identified by their legal suffix ("... Private Limited", "... LLP").

    Finds the suffix, then walks left over capitalised tokens (plus "&", "of",
    "and" connectors) to recover the full name. This is far more precise than
    spaCy's ORG label on legal text.
    """

    SUFFIX_RE = re.compile(
        r"(?:,\s*)?\b(?:Private\s+Limited|Pvt\.?\s+Ltd\.?|Limited|Ltd\.|LLP|L\.L\.P\.|Family\s+Trust|"
        r"Trust|Foundation|Bank(?:\s+of\s+India)?|Associates|N\.A\.)(?=[\s,;:)\]”’\"'.]|$)"
    )
    SUFFIX_WORDS = {"limited", "ltd", "ltd.", "llp", "trust", "foundation", "bank", "associates", "n.a.", "private", "pvt", "pvt."}
    TOKEN_RE = re.compile(r"[A-Za-z0-9][\w'’.\-]*|&")
    CONNECTORS = {"&", "of", "and"}
    STOP = {
        "formerly", "our", "by", "from", "with", "in", "at", "to", "as", "namely", "its", "their",
        "shares", "share", "equity", "offer", "bid", "bids", "investors", "prospectus", "act",
        "regulations", "promoter", "promoters", "shareholders", "company", "companies", "contact",
        "person", "designated", "registrar", "bankers", "sponsor", "escrow", "collection", "account",
        "public", "refund", "counsel", "law", "auditors", "statutory", "legal", "members", "syndicate",
        "telephone", "email", "e-mail", "website", "address", "name", "note", "entities", "above",
        "opp", "opposite", "near", "behind", "next", "the", "a", "an", "for", "director", "directors",
        "term", "sponsored", "scheduled", "commercial", "cooperative", "co-operative", "any", "such",
    }

    def __init__(self):
        super().__init__(supported_entities=["ORG_SUFFIX"], name="OrganizationSuffixRecognizer", supported_language="en")

    def load(self) -> None:  # nothing to load
        pass

    def analyze(self, text: str, entities: list[str], nlp_artifacts: NlpArtifacts | None = None):
        results: list[RecognizerResult] = []
        for m in self.SUFFIX_RE.finditer(text):
            start = self._walk_left(text, m.start())
            if start is None:
                continue
            end = m.end()
            # "HDFC Bank Limited": absorb an immediately following suffix.
            gap = len(text[end:]) - len(text[end:].lstrip(" \t"))
            nxt = self.SUFFIX_RE.match(text, end + gap)
            if nxt:
                end = nxt.end()
            span = text[start:end]
            if len(span.split()) < 2 and not re.search(r"\b(?:Citibank)\b", span):
                continue
            results.append(_result("ORG_SUFFIX", start, end, 0.85, self))
        return results

    def _walk_left(self, text: str, suffix_start: int) -> int | None:
        tokens = list(self.TOKEN_RE.finditer(text[max(0, suffix_start - 160):suffix_start]))
        offset = max(0, suffix_start - 160)
        start = None
        count = 0
        prev_start = suffix_start
        for tok in reversed(tokens):
            word = tok.group(0)
            gap = text[offset + tok.end():prev_start]
            if gap.strip(" \t") not in ("", "-"):
                break  # punctuation such as "," or "(" ends the name
            low = word.casefold().rstrip(".")
            if low in self.STOP or low in self.SUFFIX_WORDS:
                break
            if word in self.CONNECTORS or low in self.CONNECTORS:
                if start is None:
                    break
            elif not (word[0].isupper() or word[0].isdigit()):
                break
            start = offset + tok.start()
            prev_start = start
            count += 1
            if count >= 8:
                break
        if start is None:
            return None
        # Drop dangling leading connectors ("of KSH International Limited").
        while True:
            m = re.match(r"(?:&|of|and)\s+", text[start:suffix_start])
            if not m:
                break
            start += m.end()
        return start if start < suffix_start else None


class ContactPersonRecognizer(EntityRecognizer):
    """Names following an explicit label, e.g. "Contact Person: Lokesh Shah/ Soumavo Sarkar"."""

    LABEL_RE = re.compile(r"(?i:contact\s+persons?|name\s+of\s+the\s+contact\s+person)\s*:?\s*")
    TOKEN_RE = re.compile(r"[A-Z][a-zA-Z'’.-]*\.?")
    LABEL_WORDS = {"sebi", "registration", "no", "no.", "cin", "website", "email", "e-mail", "telephone",
                   "tel", "investor", "grievance", "address", "designation", "fax", "mobile", "phone"}
    MAX_TOKENS = 5

    def __init__(self):
        super().__init__(supported_entities=["PERSON_LABELLED"], name="ContactPersonRecognizer", supported_language="en")

    def load(self) -> None:
        pass

    def analyze(self, text: str, entities: list[str], nlp_artifacts: NlpArtifacts | None = None):
        results = []
        for label in self.LABEL_RE.finditer(text):
            pos = label.end()
            while True:
                end = self._name_end(text, pos)
                if end is None:
                    break
                results.append(_result("PERSON_LABELLED", pos, end, 0.9, self))
                sep = re.match(r"\s*(?:/|,|and\b)\s*", text[end:])
                if not sep:
                    break
                pos = end + sep.end()
        return results

    def _name_end(self, text: str, pos: int) -> int | None:
        """Capitalised tokens up to a label word or an all-caps token after title-case ones."""
        end, count, seen_title = None, 0, False
        while count < self.MAX_TOKENS:
            m = self.TOKEN_RE.match(text, pos)
            if not m:
                break
            word = m.group(0)
            if word.casefold().rstrip(".") in self.LABEL_WORDS or word.casefold() in self.LABEL_WORDS:
                break
            is_caps = word.rstrip(".").isupper() and len(word.rstrip(".")) > 1
            if is_caps and seen_title:
                break  # "Shanti Gopalkrishnan SEBI ..." -> stop before "SEBI"
            seen_title = seen_title or not is_caps
            end, count = m.end(), count + 1
            gap = re.match(r"[ \t]+", text[end:])
            if not gap:
                break
            pos = end + gap.end()
        return end


_STATE_RE = "|".join(re.escape(s) for s in sorted(INDIAN_STATES, key=len, reverse=True))


class AddressRecognizer(EntityRecognizer):
    """Indian postal addresses anchored on a "City – PIN" or "City, State, India" tail.

    The address is extended left until a hard boundary (a label such as
    "E-mail:", a colon, the word "at", the end of a company name, a sentence
    end) and right over an optional "State, India".
    """

    PIN_ANCHOR = re.compile(
        r"(?P<city>\b[A-Z][A-Za-z]+(?:[ -][A-Z][A-Za-z]+)?)\s*,?\s*(?:[–—-]\s*)?(?P<pin>[1-8]\d[\dlI]\s?\d{3})(?!\d|[,.]\d)"
    )
    STATE_ANCHOR = re.compile(rf"(?P<city>\b[A-Z][A-Za-z]+)[ ,]+(?P<state>{_STATE_RE})[ ,]+India\b")
    US_ANCHOR = re.compile(
        r"(?P<city>\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)?),\s+(?:A[KLRZ]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|"
        r"N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\s+\d{5}(?:-\d{4})?\b"
    )
    TAIL = re.compile(rf"(?:\s*,?\s*\(?(?:{_STATE_RE})\)?)?(?:\s*,?\s*India)?")
    ID_CODE = re.compile(r"\b[A-Z]{2,8}-\d{6}$")  # ticket/reference numbers ("TCK-200103"), not "City-PIN"
    CITY_STOP = {"Fiscal", "Fiscals", "Rule", "Rules", "Regulation", "Section", "Page", "Pages", "Note", "Notes",
                 "Unit", "Plot", "No", "Floor", "Form", "Schedule", "Clause", "Sr", "Ind", "Number", "Numbers"}
    LEFT_BOUNDARY = re.compile(
        r"(?:\b(?:Telephone|Tel|Phone|Fax|E-?mail|Email|Website|Contact\s+Person|CIN|SEBI\s+Registration)\b[^:]*:\s*\S+"
        r"|:\s|;\s|\|\s*|\n|\bat\s+|\baddress\s+|(?<![Nn]ext\s)\bto\s+|\bLimited\b,?\s*|\bLLP\b,?\s*|\bBank\b,?\s+(?=[A-Z0-9])"
        r"|\)\s+(?=[A-Z0-9])|(?<!\bNo)(?<!\bno)(?<!\bS)(?<!\bOpp)(?<![A-Z])\.\s+(?=[A-Z]))"
    )
    ADDRESS_CUES = re.compile(
        r"\d|\b(?:Road|Rd|Marg|Nagar|Village|Taluka|District|Plot|Tower|Building|Floor|Wing|Complex|Park|Centre|Center|"
        r"Street|Lane|Chowk|Peth|Area|Estate|House|Society|Apartment|Colony|Chambers|Bhavan|Block|Station|Near|Opp|"
        r"Opposite|Behind|Campus|Level|Sector|Gymkhana|Bunglow|Bungalow|Residency|Kurla|East|West|"
        r"Avenue|Ave|Suite|Drive|Blvd|Boulevard|Parkway|Way|Court|Apt)\b",
        re.IGNORECASE,
    )

    def __init__(self):
        super().__init__(supported_entities=["ADDRESS"], name="AddressRecognizer", supported_language="en")

    def load(self) -> None:
        pass

    def analyze(self, text: str, entities: list[str], nlp_artifacts: NlpArtifacts | None = None):
        results = []
        anchors = [(m.start(), m.end(), m.group("city")) for m in self.PIN_ANCHOR.finditer(text)]
        anchors += [(m.start(), m.end(), m.group("city")) for m in self.STATE_ANCHOR.finditer(text)]
        anchors += [(m.start(), m.end(), m.group("city")) for m in self.US_ANCHOR.finditer(text)]
        for a_start, a_end, city in sorted(anchors):
            if city.split()[0] in self.CITY_STOP or self.ID_CODE.search(text, a_start, a_end):
                continue
            tail = self.TAIL.match(text, a_end)
            end = tail.end() if tail else a_end
            start = self._left_start(text, a_start)
            results.append(_result("ADDRESS", start, end, 0.8, self))
        return results

    def _left_start(self, text: str, anchor_start: int) -> int:
        window_start = max(0, anchor_start - 220)
        window = text[window_start:anchor_start]
        start = window_start
        for b in self.LEFT_BOUNDARY.finditer(window):
            start = window_start + b.end()
        if start == window_start and window_start > 0:
            # No boundary inside a long window: keep only the last clause.
            comma = window.rfind(",", 0, max(0, len(window) - 120))
            start = window_start + (comma + 1 if comma >= 0 else 0)
        segment = text[start:anchor_start]
        if segment.strip() and not self.ADDRESS_CUES.search(segment):
            return anchor_start  # preceding text is prose, not an address line
        while start < anchor_start and text[start] in " \t,;-–":
            start += 1
        return start


def custom_recognizers() -> Iterable[EntityRecognizer]:
    from presidio_analyzer.predefined_recognizers import InPanRecognizer

    return [
        IndianPhoneRecognizer(),
        BrokenUrlRecognizer(),
        AadhaarRecognizer(),
        InPanRecognizer(),
        DinRecognizer(),
        DateOfBirthRecognizer(),
        OrganizationSuffixRecognizer(),
        ContactPersonRecognizer(),
        AddressRecognizer(),
    ]
