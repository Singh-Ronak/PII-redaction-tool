import pytest

from pii_redactor.analyzer import PIIDetector
from pii_redactor.config import RedactionConfig


@pytest.fixture(scope="session")
def detector() -> PIIDetector:
    """Loading spaCy en_core_web_lg takes a few seconds, so share one detector."""
    return PIIDetector(RedactionConfig())


def detected(detector: PIIDetector, text: str, context=()) -> list[tuple[str, str]]:
    return [(s.entity_type, text[s.start:s.end]) for s in detector.detect(text, tuple(context))]
