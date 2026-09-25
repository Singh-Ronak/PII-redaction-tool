"""End-to-end .docx redaction: detect -> verify -> propagate -> replace in place."""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import docx
import pytesseract

from .analyzer import PIIDetector
from .config import ORG_KEYWORDS, PUBLIC_BODIES, ROLE_AND_HEADER_TERMS, RedactionConfig
from .docx_text import (
    TextUnit,
    apply_replacements,
    iter_text_units,
    redact_field_codes,
    redact_hyperlink_targets,
    scrub_core_properties,
)
from .faker_map import ReplacementMap, match_case, strip_org_suffix
from .image_redactor import ImageRedactor
from .models import Span
from .postprocess import GENERIC_WORDS, is_vocabulary_span, resolve_overlaps, url_is_public
from .recognizers import AddressRecognizer, OrganizationSuffixRecognizer

CONTINUATION = "continuation"
SHORT_FORM = "short_form"
_LOWER_WORD = re.compile(r"(?<![\w@./:-])[a-z][a-z]+(?:-[a-z]+)*(?![\w@]|\.\w)")


def _faker_name_words() -> frozenset[str]:
    from faker.providers.person.en_IN import Provider as IndianNames
    from faker.providers.person.en_US import Provider as USNames

    names = list(IndianNames.first_names) + list(IndianNames.last_names)
    names += list(USNames.first_names) + list(USNames.last_names)
    return frozenset(n.casefold() for n in names)


NAME_WORDS = _faker_name_words()


def document_vocabulary(texts) -> set[str]:
    """Words written in lower case somewhere in the document (outside e-mails/URLs)."""
    return {m.group(0) for t in texts for m in _LOWER_WORD.finditer(t)}

log = logging.getLogger(__name__)

_LABEL_LINE = re.compile(
    r"(?i)\b(?:telephone|tel|e-?mail|website|contact\s+person|sebi\s+registration|cin|firm\s+registration|peer\s+review)\b"
)
_STATE_ONLY = re.compile(
    r"^\s*\(?(?:[A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\)?,?\s*(?:India)?\.?\s*$"
)


@dataclass
class DocumentDetections:
    units: list[TextUnit]
    spans: list[list[Span]]
    known: dict[str, str] = field(default_factory=dict)  # normalised value -> entity type

    def by_type(self) -> Counter:
        return Counter(s.entity_type for spans in self.spans for s in spans)


class DocxRedactor:
    def __init__(self, config: RedactionConfig | None = None, detector: PIIDetector | None = None):
        self.config = config or RedactionConfig()
        self.detector = detector or PIIDetector(self.config)
        self._known_re: re.Pattern | None = None
        self._known: dict[str, str] = {}

    # ---------------------------------------------------------- detection
    def detect_document(self, document) -> DocumentDetections:
        units = list(iter_text_units(document))
        spans = [self.detector.detect(u.text, u.context) for u in units]
        self._drop_vocabulary_names(units, spans)
        self._extend_address_blocks(units, spans)
        self._drop_public_body_addresses(units, spans)
        self._propagate_known_entities(units, spans)
        self._propagate_short_forms(units, spans)
        self._join_split_names(units, spans)
        return DocumentDetections(units, spans, dict(self._known))

    def _drop_vocabulary_names(self, units: list[TextUnit], spans: list[list[Span]]) -> None:
        """Remove spaCy PERSON/ORG spans made only of words the document also uses in lower case."""
        document_words = self._document_words = document_vocabulary(u.text for u in units)
        dictionary = self._dictionary = self.detector.english_words()
        for i, unit in enumerate(units):
            spans[i] = [
                s for s in spans[i]
                if not (s.source == "SpacyRecognizer" and s.entity_type in ("PERSON", "ORGANIZATION")
                        and is_vocabulary_span(unit.text[s.start:s.end], document_words, dictionary, NAME_WORDS))
            ]

    def _drop_public_body_addresses(self, units: list[TextUnit], spans: list[list[Span]]) -> None:
        """Offices of regulators/authorities ("Registrar of Companies, Maharashtra at Pune" + address) are kept."""
        for i in range(1, len(units)):
            previous = re.sub(r"\s+", " ", units[i - 1].text).strip(" ,:;").casefold()
            if previous in PUBLIC_BODIES and any(s.entity_type == "ADDRESS" for s in spans[i]):
                spans[i] = [s for s in spans[i] if s.entity_type != "ADDRESS"]

    def _propagate_short_forms(self, units: list[TextUnit], spans: list[list[Span]]) -> None:
        """Stand-alone brand names: "Nuvama" after "Nuvama Wealth Management Limited" was found.

        Only the first word of a legal-suffix company name is used, and only if
        it is distinctive: not an English/document word, not a person-name
        token, at least 4 letters. It is replaced by the first word of the
        full name's fake (``Span.value`` holds the full name, source ``SHORT_FORM``).
        """
        person_tokens = {t for k, v in self._known.items() if v == "PERSON" for t in k.split()}
        short: dict[str, str] = {}
        for unit, unit_spans in zip(units, spans):
            for s in unit_spans:
                if s.source != "OrganizationSuffixRecognizer":
                    continue
                full = re.sub(r"\s+", " ", unit.text[s.start:s.end]).strip()
                bare = strip_org_suffix(full)
                bare_tokens = [t.casefold() for t in bare.split()]
                if (len(bare_tokens) >= 2 and bare.casefold() not in PUBLIC_BODIES
                        and not all(t in GENERIC_WORDS or t in ORG_KEYWORDS or t in {"&", "of", "and"} for t in bare_tokens)):
                    short.setdefault(bare.casefold(), full)  # "Waterloo Motors" (table cell) -> full company name
                first = full.split()[0]
                key = first.casefold()
                min_len = 3 if first.isupper() else 4  # "KSH" (brand acronym), but not "Arm"
                if (len(first) < min_len or not re.fullmatch(r"[A-Za-z][A-Za-z-]*", first) or key in person_tokens
                        or key in self._document_words or key in self._dictionary or key in NAME_WORDS
                        or key in ROLE_AND_HEADER_TERMS or key in PUBLIC_BODIES or first.islower()):
                    continue
                short.setdefault(key, full)
        if not short:
            return
        alternatives = sorted(short, key=len, reverse=True)
        pattern = re.compile(
            r"(?<![\w@./-])(?:" + "|".join(r"\s+".join(map(re.escape, k.split())) for k in alternatives)
            + r")(?![\w@-]|\.\w)",
            re.IGNORECASE,
        )
        for i, unit in enumerate(units):
            extra = [
                Span(m.start(), m.end(), "ORGANIZATION", 0.7, SHORT_FORM,
                     value=short[re.sub(r"\s+", " ", m.group(0)).casefold()])
                for m in pattern.finditer(unit.text)
                if not m.group(0).islower() and not any(m.start() < o.end and o.start < m.end() for o in spans[i])
            ]
            if extra:
                spans[i] = sorted(spans[i] + extra, key=lambda s: s.start)

    def _join_split_names(self, units: list[TextUnit], spans: list[list[Span]]) -> None:
        """A known name wrapped over two paragraphs ("... EVEREST" / "FAMILY TRUST, ...").

        The fake goes where the name starts; the continuation in the next
        paragraph is blanked (``Span`` with source ``CONTINUATION``).
        """
        if not self._known_re:
            return
        for i in range(len(units) - 1):
            a, b = units[i], units[i + 1]
            if a.container is not b.container:
                continue
            tail_start = max(0, len(a.text) - 80)
            joined = a.text[tail_start:] + " " + b.text[:80]
            boundary = len(a.text) - tail_start
            for m in self._known_re.finditer(joined):
                if not (m.start() < boundary < m.end()):
                    continue
                a_s, a_e = tail_start + m.start(), len(a.text.rstrip())
                b_e = m.end() - boundary - 1
                b_s = len(b.text) - len(b.text.lstrip())
                if b_e <= b_s or a_e <= a_s:
                    continue
                value = re.sub(r"\s+", " ", m.group(0))
                full = Span(a_s, a_e, self._known.get(value.casefold(), "PERSON"), 0.8, "propagation", value=value)
                spans[i] = [s for s in spans[i] if not s.overlaps(full)] + [full]
                spans[i].sort(key=lambda s: s.start)
                cont = Span(b_s, b_e, full.entity_type, 0.8, CONTINUATION)
                spans[i + 1] = sorted([s for s in spans[i + 1] if not s.overlaps(cont)] + [cont],
                                      key=lambda s: s.start)

    def _extend_address_blocks(self, units: list[TextUnit], spans: list[list[Span]]) -> None:
        """Multi-line addresses: pull preceding address lines / trailing 'State, India' into the block."""
        for i, unit in enumerate(units):
            addresses = [s for s in spans[i] if s.entity_type == "ADDRESS"]
            if not addresses:
                continue
            first, last = min(addresses, key=lambda s: s.start), max(addresses, key=lambda s: s.end)
            if unit.text[:first.start].strip() == "":
                for j in range(i - 1, max(-1, i - 4), -1):
                    if units[j].container is not unit.container:
                        break
                    piece = _address_fragment(units[j].text)
                    if piece is None:
                        break
                    s, e = piece
                    spans[j] = resolve_overlaps(spans[j] + [Span(s, e, "ADDRESS", 0.7, "address_block")])
                    if s > 0:
                        break  # the line started with a company name/label: block starts here
            if unit.text[last.end:].strip() == "" and i + 1 < len(units):
                nxt = units[i + 1]
                if nxt.container is unit.container and _STATE_ONLY.match(nxt.text) and "India" in nxt.text:
                    s = len(nxt.text) - len(nxt.text.lstrip())
                    e = len(nxt.text.rstrip())
                    spans[i + 1] = resolve_overlaps(spans[i + 1] + [Span(s, e, "ADDRESS", 0.7, "address_block")])

    def _propagate_known_entities(self, units: list[TextUnit], spans: list[list[Span]]) -> None:
        """Find every other mention (any letter case) of names/companies detected somewhere."""
        known: dict[str, str] = {}
        for unit, unit_spans in zip(units, spans):
            for s in unit_spans:
                if s.entity_type not in ("PERSON", "ORGANIZATION"):
                    continue
                value = re.sub(r"\s+", " ", unit.text[s.start:s.end]).strip()
                key = value.casefold()
                if len(value.split()) < 2 or len(value) < 6:
                    continue
                if key in ROLE_AND_HEADER_TERMS or key in PUBLIC_BODIES:
                    continue
                known.setdefault(key, s.entity_type)
        self._known = known
        if not known:
            self._known_re = None
            return
        alternatives = sorted(known, key=len, reverse=True)
        self._known_re = re.compile(
            r"(?<![\w])(?:" + "|".join(r"\s+".join(map(re.escape, k.split())) for k in alternatives) + r")(?![\w])",
            re.IGNORECASE,
        )
        for i, unit in enumerate(units):
            extra = []
            for s, e, etype in self.known_matches(unit.text):
                overlapping = [o for o in spans[i] if s < o.end and o.start < e]
                # A known full name may replace a shorter name span it contains ("Rakhi Girija" -> "Rakhi Girija Shetty").
                if all(o.entity_type in ("PERSON", "ORGANIZATION") and s <= o.start and o.end <= e and o.length < e - s
                       for o in overlapping):
                    extra.append(Span(s, e, etype, 0.8, "propagation"))
            if extra:
                spans[i] = resolve_overlaps(spans[i] + extra)

    def known_matches(self, text: str) -> list[tuple[int, int, str]]:
        if not self._known_re or not text:
            return []
        out = []
        for m in self._known_re.finditer(text):
            if m.group(0).islower():
                continue  # "no green shoe option": ordinary prose, not a proper name
            key = re.sub(r"\s+", " ", m.group(0)).casefold()
            out.append((m.start(), m.end(), self._known.get(key, "PERSON")))
        return out

    # ------------------------------------------------------------ redaction
    def redact(self, input_path: str | Path, output_path: str | Path, report_path: str | Path | None = None) -> dict:
        t0 = time.time()
        input_path, output_path = Path(input_path), Path(output_path)
        if not input_path.is_file():
            raise FileNotFoundError(f"Input file not found: {input_path}")
        if input_path.suffix.lower() != ".docx":
            raise ValueError(f"Expected a .docx file, got: {input_path.name}")
        try:
            document = docx.Document(str(input_path))
        except Exception as exc:
            raise ValueError(f"Could not open {input_path.name} as a Word document: {exc}") from exc
        if self.config.redact_images:
            _check_tesseract(self.config)

        rmap = ReplacementMap(self.config.faker_locale, self.config.seed)
        log.info("Detecting PII in text ...")
        detections = self.detect_document(document)

        # Persons first so e-mail local parts ("siddharth.jadhav") can mirror the fake name.
        for unit, unit_spans in zip(detections.units, detections.spans):
            for s in unit_spans:
                if s.entity_type == "PERSON" and s.source != CONTINUATION:
                    rmap.replace("PERSON", s.value or unit.text[s.start:s.end])

        log.info("Replacing text in place ...")
        for unit, unit_spans in zip(detections.units, detections.spans):
            if unit_spans:
                text = unit.text
                apply_replacements(unit.segments, [(s.start, s.end, _fake_for(rmap, s, text)) for s in unit_spans])

        replace_email = lambda e: rmap.replace("EMAIL_ADDRESS", e)  # noqa: E731
        replace_url = lambda u: u if url_is_public(u) else rmap.replace("URL", u)  # noqa: E731
        field_codes = redact_field_codes(document, replace_email, replace_url)
        link_targets = redact_hyperlink_targets(document, replace_email, replace_url)
        scrub_core_properties(document)

        image_reports = []
        if self.config.redact_images:
            log.info("Redacting images ...")
            image_reports = self._redact_images(document)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        document.save(str(output_path))

        report = {
            "input": input_path.name,
            "output": output_path.name,
            "seconds": round(time.time() - t0, 1),
            "text_units": len(detections.units),
            "text_units_with_pii": sum(1 for s in detections.spans if s),
            "text_redactions_by_type": dict(sorted(detections.by_type().items())),
            "text_redactions_by_source": dict(sorted(Counter(
                s.source for spans in detections.spans for s in spans).items())),
            "unique_values_replaced": len(rmap),
            "field_codes_rewritten": field_codes,
            "hyperlink_targets_rewritten": link_targets,
            "images": image_reports,
        }
        if report_path:
            Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
        if self.config.mapping_path:
            Path(self.config.mapping_path).write_text(json.dumps(rmap.mapping(), indent=2, ensure_ascii=False),
                                                      encoding="utf-8")
        return report

    def _redact_images(self, document) -> list[dict]:
        redactor = ImageRedactor(self.detector, self.config, self.known_matches)
        reports = []
        seen = set()
        for part in document.part.package.iter_parts():
            if not part.content_type.startswith("image/") or part.partname in seen:
                continue
            seen.add(part.partname)
            new_blob, rep = redactor.redact(part.blob)
            rep["part"] = str(part.partname)
            if new_blob is not None:
                part._blob = new_blob  # overwrite the image binary in place; relationships stay intact
                rep["modified"] = True
            else:
                rep["modified"] = False
            reports.append(rep)
        return reports


def _fake_for(rmap: ReplacementMap, span: Span, text: str) -> str:
    original = text[span.start:span.end]
    if span.source == CONTINUATION:
        return ""  # the fake for the whole name was written where the name starts
    if span.source == SHORT_FORM:
        fake = rmap.replace(span.entity_type, span.value)
        short = fake.split()[0] if len(original.split()) == 1 else strip_org_suffix(fake)
        return match_case(original, short)
    return rmap.replace(span.entity_type, span.value or original)


def _address_fragment(text: str) -> tuple[int, int] | None:
    """If ``text`` is (or ends with) an address line, return the address part's offsets."""
    stripped = text.strip()
    if not stripped or len(stripped) > 220 or _LABEL_LINE.search(text):
        return None
    start = 0
    colon = text.rfind(": ")
    if colon >= 0:
        start = colon + 2
    for m in OrganizationSuffixRecognizer.SUFFIX_RE.finditer(text):
        if m.end() > start:
            start = m.end()
    fragment = text[start:]
    if not fragment.strip() or not AddressRecognizer.ADDRESS_CUES.search(fragment):
        return None
    # Prose sentences are not address lines.
    lower_words = re.findall(r"\b[a-z]{4,}\b", fragment)
    if len(lower_words) > 3:
        return None
    s = start + (len(fragment) - len(fragment.lstrip(" ,;–-")))
    e = len(text.rstrip(" ,;"))
    return (s, e) if e > s else None


def _check_tesseract(config: RedactionConfig) -> None:
    if config.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = config.tesseract_cmd
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:
        raise RuntimeError(
            "Tesseract OCR was not found. Install it (Windows: https://github.com/UB-Mannheim/tesseract/wiki) "
            "and pass --tesseract-cmd \"C:\\Program Files\\Tesseract-OCR\\tesseract.exe\", "
            "or run with --no-images to redact text only."
        ) from exc
