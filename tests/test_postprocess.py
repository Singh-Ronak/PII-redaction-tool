from pii_redactor.models import Span
from pii_redactor.postprocess import is_vocabulary_span, resolve_overlaps

from .conftest import detected


def test_resolve_overlaps_keeps_longest_then_priority():
    spans = [
        Span(0, 10, "PERSON", 0.9, "a"),
        Span(0, 20, "ADDRESS", 0.7, "b"),
        Span(25, 30, "PERSON", 0.6, "c"),
        Span(25, 30, "EMAIL_ADDRESS", 0.6, "d"),
    ]
    kept = resolve_overlaps(spans)
    assert [(s.start, s.end, s.entity_type) for s in kept] == [(0, 20, "ADDRESS"), (25, 30, "EMAIL_ADDRESS")]


def test_roles_are_not_people(detector):
    text = "Sarthak Malvadkar, Company Secretary and Compliance Officer, and the Managing Director"
    assert detected(detector, text) == [("PERSON", "Sarthak Malvadkar")]


def test_public_bodies_are_kept(detector):
    text = "The Equity Shares will be listed on BSE Limited and National Stock Exchange of India Limited."
    assert detected(detector, text) == []


def test_defined_terms_are_not_names(detector):
    assert detected(detector, "This Red Herring Prospectus is filed with the RoC.") == []


def test_vocabulary_filter():
    dictionary = frozenset({"allot", "basis", "selling", "green", "shoe", "option"})
    names = frozenset({"green", "rajesh"})
    assert is_vocabulary_span("Allotted", set(), dictionary, names)
    assert is_vocabulary_span("Basis", set(), dictionary, names)
    assert not is_vocabulary_span("Rajesh Hegde", set(), dictionary, names)
    # "Green" is also a surname: only ordinary when the document itself writes it in lower case.
    assert not is_vocabulary_span("Green Shoe Option", set(), dictionary, names)
    assert is_vocabulary_span("Green Shoe Option", {"green"}, dictionary, names)
