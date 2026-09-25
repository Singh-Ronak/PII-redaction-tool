"""Presidio analyzer construction and the text-level detection API."""

from __future__ import annotations

import logging
from functools import lru_cache

from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
from presidio_analyzer.nlp_engine import NlpEngineProvider

from .config import RedactionConfig
from .models import Span
from .postprocess import verify
from .recognizers import custom_recognizers

log = logging.getLogger(__name__)


def build_analyzer(spacy_model: str = "en_core_web_lg") -> AnalyzerEngine:
    """Create an AnalyzerEngine backed by spaCy ``en_core_web_lg``.

    The engine is configured only through supported constructor arguments
    (``nlp_engine`` and ``registry``). Allow-lists and false-positive filters
    are applied afterwards by :func:`pii_redactor.postprocess.verify`.
    """
    provider = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": spacy_model}],
        }
    )
    nlp_engine = provider.create_engine()
    registry = RecognizerRegistry()
    registry.load_predefined_recognizers(nlp_engine=nlp_engine)
    for recognizer in custom_recognizers():
        registry.add_recognizer(recognizer)
    return AnalyzerEngine(nlp_engine=nlp_engine, registry=registry)


class PIIDetector:
    """Runs Presidio on a piece of text and returns verified, non-overlapping spans."""

    def __init__(self, config: RedactionConfig | None = None, analyzer: AnalyzerEngine | None = None):
        self.config = config or RedactionConfig()
        self.analyzer = analyzer or build_analyzer(self.config.spacy_model)
        self._cached = lru_cache(maxsize=20000)(self._detect_uncached)
        self._english_words: frozenset[str] | None = None

    def detect(self, text: str, context: tuple[str, ...] = ()) -> list[Span]:
        if not text or not text.strip():
            return []
        return list(self._cached(text, tuple(context)))

    def _detect_uncached(self, text: str, context: tuple[str, ...]) -> tuple[Span, ...]:
        # Justified Word text uses tabs between words ("Sunil\tNagayya Shetty"); spaCy treats a
        # tab as a token and splits entities there. Same-length substitution keeps offsets valid.
        text = text.replace("\t", " ")
        try:
            raw = self.analyzer.analyze(
                text=text,
                language="en",
                entities=list(self.config.entities),
                context=list(context) or None,
            )
        except Exception:  # a single bad paragraph must not abort the whole document
            log.exception("Presidio analysis failed for text of length %d", len(text))
            return ()
        return tuple(verify(text, raw, self.config))

    def english_words(self) -> frozenset[str]:
        """Dictionary words from spaCy's (WordNet-derived) lemmatizer tables; empty if unavailable."""
        if self._english_words is None:
            words: set[str] = set()
            try:
                nlp = self.analyzer.nlp_engine.nlp["en"]
                lookups = nlp.get_pipe("lemmatizer").lookups
                index = lookups.get_table("lemma_index")
                for pos in index.keys():
                    words.update(w for w in index[pos] if "_" not in w)
                exc = lookups.get_table("lemma_exc")
                for pos in exc.keys():
                    words.update(exc[pos].keys())
            except Exception:  # model without a rule-based lemmatizer
                log.warning("No lemmatizer tables in %s; dictionary-word filter disabled", self.config.spacy_model)
            self._english_words = frozenset(words)
        return self._english_words
