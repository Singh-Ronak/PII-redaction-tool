"""Post-analysis verification pass.

Presidio/spaCy output on legal documents contains many false positives:
role titles tagged as PERSON ("Company Secretary"), defined terms tagged as
ORGANIZATION ("SEBI ICDR Regulations"), person names tagged as ORGANIZATION,
and spans that swallow neighbouring labels ("Email ksh.ipo@..."). This module
trims, splits, re-labels and filters those results in plain Python.
"""

from __future__ import annotations

import re
from typing import Iterable

from presidio_analyzer import RecognizerResult

from .config import (
    INTERNAL_ENTITY_ALIASES,
    NON_ENTITY_TOKENS,
    ORG_KEYWORDS,
    PUBLIC_BODIES,
    PUBLIC_URL_DOMAINS,
    ROLE_AND_HEADER_TERMS,
    RedactionConfig,
)
from .models import Span

# Tokens that commonly get glued onto NER spans and must be trimmed off.
LABEL_TOKENS = {
    "email", "e-mail", "mail", "telephone", "tel", "phone", "mobile", "contact", "person", "persons",
    "name", "names", "pan", "din", "cin", "website", "fax", "mr", "mrs", "ms", "dr", "shri", "smt",
    "our", "the", "and", "of", "by", "to", "from", "his", "her", "their", "its", "promoter", "promoters",
    "director", "directors", "chairman", "father", "father's", "address", "dob", "sebi", "registration",
    "this", "these", "that", "those", "such", "said", "a", "an",
}

# spaCy-only ORG/PERSON spans made up solely of these words are generic.
GENERIC_WORDS = NON_ENTITY_TOKENS | {
    t.casefold() for t in (
        "Company", "Companies", "Limited", "Private", "Bank", "Banks", "Trust", "Trusts", "Group",
        "Board", "Government", "India", "Indian", "Exchange", "Exchanges", "Stock", "Registered",
        "Corporate", "Brokers", "Broker", "Depository", "Participants", "Agents", "Transfer",
        "Collecting", "Sponsor", "Syndicate", "Escrow", "Public", "Refund", "Account", "Market",
        "Markets", "Mutual", "Funds", "Life", "Insurance", "Pension", "Qualified", "Institutional",
        "Buyers", "Retail", "Individual", "Non-Institutional", "Anchor", "Designated", "Intermediary",
        "Intermediaries", "Self-Certified", "Syndicate", "Book", "Running", "Lead", "Legal", "Law",
        "Chartered", "Accountants", "Accountant", "Independent", "Executive", "Whole-time", "Chief",
        "Financial", "Joint", "Technical", "Compliance", "Key", "Managerial", "Senior", "Industry",
        "Research", "Data", "Provider", "Magnet", "Winding", "Wires", "Copper", "Aluminium", "Power",
        "Sector", "Supa", "Chakan", "Maharashtra", "Pune", "Mumbai", "Crore", "Million", "Rupees",
        "Split", "Bonus", "Restated", "ESOP", "IPO", "QIB", "NII", "RII", "BRLM", "BRLMs", "GIR",
        "Master", "General", "Information", "Document", "Price", "Cap", "Floor", "Working",
        "Income", "Tax", "Department", "Permanent", "Card", "Number", "Govt", "Govt.",
        "Collection", "Marketing", "Sales", "Operations", "Herring", "Red",
        "Pradhan", "Mantri", "Yojana", "Sabha", "Lok", "Rajya", "Urja", "Suraksha", "Kisan", "Awas",
        "Karta", "Non-GAAP", "Allottee", "Allottees",
    )
}

# A spaCy span ending in one of these is a defined/technical term as a whole
# ("Brushless Direct Current", "Inter-State Transmission System"), not a name.
TERM_TAIL_WORDS = {
    t.casefold() for t in (
        "Commission", "System", "Systems", "Transmission", "Option", "Current", "Yojana", "Scheme",
        "Mission", "Programme", "Program", "Index", "Method", "Model", "Standard",
    )
}

# A name immediately followed by one of these is part of a defined term
# ("Red Herring Prospectus", "Companies Act", "Nomination Committee").
FOLLOWING_TERM_RE = re.compile(
    r"^[\s'’s]*(?:Prospectus|Act|Regulations?|Rules?|Report|Policy|Scheme|Committee|Portion|Agreement|Circular)\b"
)

_PUBLIC_BODY_RE = re.compile(
    r"(?i)\b(?:" + "|".join(re.escape(b) for b in sorted((b for b in PUBLIC_BODIES if len(b) > 4), key=len, reverse=True)) + r")\b"
)

ENTITY_PRIORITY = [
    "EMAIL_ADDRESS", "URL", "IN_AADHAAR", "IN_PAN", "CREDIT_CARD", "US_SSN", "IP_ADDRESS",
    "PHONE_NUMBER", "DATE_OF_BIRTH", "IN_DIN", "ADDRESS", "ORGANIZATION", "PERSON",
]

# Words that make a span an address fragment rather than a person's name.
ADDRESS_WORDS = {
    t.casefold() for t in (
        "Village", "Taluka", "District", "Road", "Marg", "Nagar", "Colony", "Society", "Apartment",
        "Building", "Tower", "Floor", "Wing", "Complex", "Chowk", "Peth", "Lane", "Street", "House",
        "Chambers", "Bhavan", "Park", "Campus", "Station", "Hospital", "Hotel", "Gymkhana", "East",
        "West", "North", "South", "Block", "Sector", "Plot", "Residency", "Bunglow", "Bungalow",
    )
}

RULE_BASED_NAME_SOURCES = {"OrganizationSuffixRecognizer", "ContactPersonRecognizer"}

_WORD_RE = re.compile(r"[\w'’.&-]+")
_NAME_TOKEN_RE = re.compile(r"^(?:[A-Z][a-zA-Z'’-]*\.?|[A-Z]\.)$")


def _tokens(s: str) -> list[str]:
    return _WORD_RE.findall(s)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" \t\n,;:.-–/()").casefold()


def _recognizer(r: RecognizerResult) -> str:
    return (r.recognition_metadata or {}).get(RecognizerResult.RECOGNIZER_NAME_KEY, "unknown")


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    """Strip whitespace/punctuation and glued-on label words at both ends."""
    changed = True
    while changed and start < end:
        changed = False
        while start < end and not text[start].isalnum():
            start += 1
            changed = True
        while end > start and not (text[end - 1].isalnum() or text[end - 1] in ".)"):
            end -= 1
            changed = True
        m = re.match(r"([\w'’-]+)[\s:]+", text[start:end])
        if m and m.group(1).casefold() in LABEL_TOKENS and m.end() < end - start:
            start += m.end()
            changed = True
        m = re.search(r"[\s:]+([\w'’-]+)$", text[start:end])
        if m and m.group(1).casefold() in LABEL_TOKENS:
            end = start + m.start()
            changed = True
    return start, end


def _split_person(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """'Lokesh Shah/ Soumavo Sarkar' -> two spans."""
    parts, cursor = [], start
    for m in re.finditer(r"\s*(?:/|,|;|\band\b|\n|[ \t]{2,})\s*", text[start:end]):
        parts.append((cursor, start + m.start()))
        cursor = start + m.end()
    parts.append((cursor, end))
    return [(s, e) for s, e in parts if e > s]


def _looks_like_person(value: str) -> bool:
    toks = value.split()
    if not 1 <= len(toks) <= 5:
        return False
    if any(
        t.casefold().strip(".") in GENERIC_WORDS or t.casefold() in ORG_KEYWORDS or t.casefold() in ADDRESS_WORDS
        for t in toks
    ):
        return False
    return all(_NAME_TOKEN_RE.match(t) or t.isupper() and t.isalpha() for t in toks)


def _is_allowed(value: str) -> bool:
    n = _norm(value)
    return n in ROLE_AND_HEADER_TERMS or n in PUBLIC_BODIES


def _has_org_keyword(value: str) -> bool:
    return any(t.casefold() in ORG_KEYWORDS for t in _tokens(value))


def _tighten_spacy_org(text: str, s: int, e: int) -> tuple[int, int]:
    """'Offer Escrow Collection Bank HDFC Bank Limited Ground Floor' -> 'HDFC Bank Limited'."""
    toks = list(_WORD_RE.finditer(text[s:e]))
    kw = [i for i, t in enumerate(toks) if t.group(0).casefold() in ORG_KEYWORDS]
    if not kw:
        return s, e
    last = kw[-1]
    first = 0
    while first < last and toks[first].group(0).casefold().strip(".") in (GENERIC_WORDS | LABEL_TOKENS):
        first += 1
    return s + toks[first].start(), s + toks[last].end()


def _drop_trailing_generic(text: str, s: int, e: int) -> tuple[int, int]:
    """'Sarthak Malvadkar Company' -> 'Sarthak Malvadkar' (spaCy spans without an org keyword)."""
    toks = list(_WORD_RE.finditer(text[s:e]))
    while len(toks) > 1 and toks[-1].group(0).casefold().strip(".") in (GENERIC_WORDS | LABEL_TOKENS):
        toks.pop()
    return (s, s + toks[-1].end()) if toks else (s, e)


def _verify_name_like(text: str, r: RecognizerResult, etype: str) -> Iterable[tuple[int, int, str]]:
    """Clean PERSON / ORGANIZATION spans and decide their final type."""
    source = _recognizer(r)
    pieces = [(r.start, r.end)]
    if etype == "PERSON" or (source == "SpacyRecognizer" and "/" in text[r.start:r.end]):
        pieces = _split_person(text, r.start, r.end)
    for s, e in pieces:
        s, e = _trim(text, s, e)
        if source == "SpacyRecognizer":
            if FOLLOWING_TERM_RE.match(text[e:]):
                continue
            raw_low = [t.casefold().strip(".,") for t in _tokens(text[s:e])]
            if raw_low and raw_low[-1] in TERM_TAIL_WORDS:
                continue
            if _has_org_keyword(text[s:e]):
                s, e = _tighten_spacy_org(text, s, e)
            else:
                s, e = _drop_trailing_generic(text, s, e)
            if FOLLOWING_TERM_RE.match(text[e:]):
                continue
        value = text[s:e]
        if len(value) < 2 or not re.search(r"[A-Za-z]{2}", value) or "@" in value or "://" in value:
            continue
        if _is_allowed(value) or _PUBLIC_BODY_RE.search(value) or value.count("(") != value.count(")"):
            continue
        toks = _tokens(value)
        low = [t.casefold().strip(".,") for t in toks]
        if all(t in GENERIC_WORDS or t in LABEL_TOKENS for t in low):
            continue
        if source == "SpacyRecognizer" and all(t in GENERIC_WORDS or t in ORG_KEYWORDS for t in low):
            continue  # "Securities" cut out of "Securities Contracts (Regulation) Rules"
        if source == "OrganizationSuffixRecognizer":
            yield s, e, "ORGANIZATION"
            continue
        if any(t in NON_ENTITY_TOKENS for t in low) and not _has_org_keyword(value):
            continue
        if re.search(r"\d", value) and not _has_org_keyword(value):
            continue
        if _has_org_keyword(value):
            yield s, e, "ORGANIZATION"
        elif _looks_like_person(value):
            if source == "SpacyRecognizer" and len(toks) == 1 and (
                value.isupper() or len(value) < 3 or re.fullmatch(r"[A-Z][a-z]*[A-Z]\w{0,3}", value)
                or re.fullmatch(r"[A-Z]{2,}-\w+|\w+-[A-Z]{2,}", value)
            ):
                continue  # lone acronyms ("KSH", "SSN", "AoA", "MIRSD-PoD") are not people
            yield s, e, "PERSON"
        elif etype == "ORGANIZATION" and source == "SpacyRecognizer":
            # spaCy ORG without a legal suffix: keep only short proper-noun brands.
            if (
                1 <= len(toks) <= 4
                and all(t[:1].isupper() for t in toks)
                and not value.isupper()
                and not any(t in ADDRESS_WORDS for t in low)
            ):
                yield s, e, "ORGANIZATION"


def url_is_public(value: str) -> bool:
    host = re.sub(r"^[a-z]+://", "", value.casefold()).split("/")[0]
    return any(host == d or host.endswith("." + d) for d in PUBLIC_URL_DOMAINS)


def verify(text: str, results: Iterable[RecognizerResult], config: RedactionConfig) -> list[Span]:
    """Filter raw Presidio results into final, non-overlapping spans."""
    candidates: list[Span] = []
    for r in results:
        if r.score < config.threshold_for(r.entity_type):
            continue
        source = _recognizer(r)
        etype = INTERNAL_ENTITY_ALIASES.get(r.entity_type, r.entity_type)
        if etype in ("PERSON", "ORGANIZATION"):
            for s, e, final_type in _verify_name_like(text, r, etype):
                candidates.append(Span(s, e, final_type, r.score, source))
            continue
        s, e = r.start, r.end
        if etype == "ADDRESS":
            s, e = _trim(text, s, e)
        elif etype == "PHONE_NUMBER":
            value = text[s:e]
            s += len(value) - len(value.lstrip(" \t,;:"))
            e -= len(value) - len(value.rstrip(" \t,;:-"))
        value = text[s:e]
        if not value.strip():
            continue
        if etype == "URL":
            value = value.rstrip(".,;)")
            e = s + len(value)
            if url_is_public(value) or "." not in value or re.fullmatch(r"[\d.]+", value):
                continue
        if etype == "EMAIL_ADDRESS" and url_is_public(value.rpartition("@")[2]):
            continue  # government helpdesk addresses are not personal data
        if etype == "PHONE_NUMBER" and sum(c.isdigit() for c in value) < 8:
            continue
        candidates.append(Span(s, e, etype, r.score, source))
    return resolve_overlaps(_prefer_rule_based(candidates))


def _stems(word: str) -> set[str]:
    """Crude inflection stripping so 'allotted'/'centres' match 'allot'/'centre'."""
    out = {word}
    if word.endswith("ies") and len(word) > 5:
        out.add(word[:-3] + "y")
    for suffix, cut in (("ing", 3), ("ed", 2), ("es", 2), ("s", 1)):
        if word.endswith(suffix) and len(word) - cut >= 3:
            base = word[:-cut]
            out |= {base, base + "e"}
            if len(base) > 3 and base[-1] == base[-2]:
                out.add(base[:-1])  # allotted -> allott -> allot
    return out


def is_vocabulary_span(
    value: str, document_words: set[str], dictionary: frozenset[str], name_words: frozenset[str]
) -> bool:
    """True if every token of an NER name is an ordinary word rather than a name.

    ``document_words`` holds words that also occur in lower case somewhere in
    the document ("allotted", "green shoe option"); a real name ("Rajesh",
    "Malvadkar") essentially never does. ``dictionary`` is an English word list;
    dictionary words that are also common first/last names ("Bill", "Shah")
    are only treated as ordinary when the document itself uses them in lower case.
    """
    def ordinary(tok: str) -> bool:
        low = tok.casefold().strip(".")
        if low in GENERIC_WORDS or low in LABEL_TOKENS or low in document_words:
            return True
        if low in name_words:
            return False
        is_acronym = tok.isupper() and tok.isalpha() and len(tok) <= 5 and not value.isupper()
        return is_acronym or any(stem in dictionary for stem in _stems(low))

    toks = _tokens(value)
    return bool(toks) and all(ordinary(t) for t in toks)


def _prefer_rule_based(spans: list[Span]) -> list[Span]:
    """Drop spaCy name spans that overlap a rule-based (suffix/label) name span."""
    rules = [sp for sp in spans if sp.source in RULE_BASED_NAME_SOURCES]
    return [
        sp for sp in spans
        if not (sp.source == "SpacyRecognizer" and any(sp.overlaps(r) for r in rules))
    ]


def resolve_overlaps(spans: Iterable[Span]) -> list[Span]:
    """Keep the longest span of any overlapping group; ties by priority, then score."""
    prio = {t: i for i, t in enumerate(ENTITY_PRIORITY)}
    ordered = sorted(
        spans, key=lambda sp: (-sp.length, prio.get(sp.entity_type, len(prio)), -sp.score, sp.start)
    )
    kept: list[Span] = []
    for sp in ordered:
        if not any(sp.overlaps(k) for k in kept):
            kept.append(sp)
    return sorted(kept, key=lambda sp: sp.start)
