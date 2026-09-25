import pytest

from pii_redactor.recognizers import (
    AddressRecognizer,
    ContactPersonRecognizer,
    OrganizationSuffixRecognizer,
    verhoeff_valid,
)

from .conftest import detected


def spans(recognizer, text):
    return [text[r.start:r.end] for r in recognizer.analyze(text, [])]


def test_verhoeff():
    assert verhoeff_valid("2363")  # textbook example
    assert not verhoeff_valid("2364")


@pytest.mark.parametrize("text, expected", [
    ("Our Company was incorporated as “Bhandary Metal Extrusion Private Limited” under the Act",
     ["Bhandary Metal Extrusion Private Limited"]),
    ("ICICI\tSecurities Limited", ["ICICI\tSecurities Limited"]),
    ("Escrow Bank: Federal Bank Limited, Mumbai", ["Federal Bank Limited"]),
    ("Kirtane & Pandit LLP, Chartered Accountants", ["Kirtane & Pandit LLP"]),
    ("the Short Term Bank loans", []),
])
def test_org_suffix(text, expected):
    assert spans(OrganizationSuffixRecognizer(), text) == expected


def test_contact_person_stops_at_labels():
    text = "Contact Person: Shanti Gopalkrishnan SEBI Registration No.: INR000004058"
    assert spans(ContactPersonRecognizer(), text) == ["Shanti Gopalkrishnan"]
    assert spans(ContactPersonRecognizer(), "Contact Person: Lokesh Shah/ Soumavo Sarkar") == [
        "Lokesh Shah", "Soumavo Sarkar"]


@pytest.mark.parametrize("text, expected", [
    ("Registered Office: 11/3 Village Birdewadi, Chakan, Pune – 410 501, Maharashtra, India; CIN",
     ["11/3 Village Birdewadi, Chakan, Pune – 410 501, Maharashtra, India"]),
    ("12 Buena Monte, NCL society, Pashan, Pune – 411 008, Maharashtra, India",
     ["12 Buena Monte, NCL society, Pashan, Pune – 411 008, Maharashtra, India"]),
    ("Ship to 742 Evergreen Terrace, Springfield, IL 62704 by Friday.", ["742 Evergreen Terrace, Springfield, IL 62704"]),
    ("Ticket TCK-104233 was opened", []),
    ("Revenue in Fiscal 2025 grew by 410 501 units", []),
])
def test_address(text, expected):
    assert spans(AddressRecognizer(), text) == expected


def test_dob_requires_cue_in_same_sentence(detector):
    text = "Customer DOB: 14/07/1986. Account opened 03/02/2019."
    assert ("DATE_OF_BIRTH", "14/07/1986") in detected(detector, text)
    assert ("DATE_OF_BIRTH", "03/02/2019") not in detected(detector, text)
    assert detected(detector, "Meera Iyer was born on 3 January 1979.")[-1] == ("DATE_OF_BIRTH", "3 January 1979")
    assert detected(detector, "The Offer closes on 18/12/2025.") == []


def test_dob_from_table_header_context(detector):
    assert detected(detector, "21/09/1992", context=("Date", "of", "Birth")) == [("DATE_OF_BIRTH", "21/09/1992")]
    assert detected(detector, "21/09/1992") == []


def test_identifiers(detector):
    text = "SSN 536-22-8741, card 4111 1111 1111 1111, IP 203.0.113.45, phone +91 98765 43210."
    found = dict((t, v) for t, v in detected(detector, text))
    assert found["US_SSN"] == "536-22-8741"
    assert found["CREDIT_CARD"] == "4111 1111 1111 1111"
    assert found["IP_ADDRESS"] == "203.0.113.45"
    assert found["PHONE_NUMBER"] == "+91 98765 43210"


def test_order_and_ticket_numbers_are_kept(detector):
    assert detected(detector, "Order 4500123456 and invoice INV-2024-00871 were refunded under ticket TCK-200101.") == []


def test_din_needs_context(detector):
    assert detected(detector, "00135070", context=("DIN",)) == [("IN_DIN", "00135070")]
    assert detected(detector, "00135070") == []
