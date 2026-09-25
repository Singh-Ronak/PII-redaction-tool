from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """A verified PII detection inside one text unit (character offsets)."""

    start: int
    end: int
    entity_type: str
    score: float
    source: str  # recognizer name, "propagation" or "address_block"
    # Original value to map when it differs from the text slice (a name split over two paragraphs).
    value: str | None = None

    def overlaps(self, other: "Span") -> bool:
        return self.start < other.end and other.start < self.end

    @property
    def length(self) -> int:
        return self.end - self.start
