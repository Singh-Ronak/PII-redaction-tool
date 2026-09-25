import re

from pii_redactor.faker_map import ReplacementMap, _luhn_valid
from pii_redactor.recognizers import verhoeff_valid


def test_same_value_same_fake_across_case_and_spacing():
    m = ReplacementMap(seed=1)
    a = m.replace("PERSON", "Kushal Subbayya Hegde")
    assert m.replace("PERSON", "KUSHAL SUBBAYYA HEGDE") == a.upper()
    assert m.replace("PERSON", "Kushal  Subbayya\tHegde") == a
    assert len(a.split()) == 3
    assert m.replace("PERSON", "Rajesh Kushal Hegde") != a


def test_seed_makes_output_reproducible():
    assert ReplacementMap(seed=7).replace("PERSON", "Rashi Patil") == ReplacementMap(seed=7).replace("PERSON", "Rashi Patil")


def test_organization_keeps_legal_suffix():
    m = ReplacementMap(seed=1)
    assert m.replace("ORGANIZATION", "Kirtane & Pandit LLP").endswith(" LLP")
    assert m.replace("ORGANIZATION", "Dhaulagiri Family Trust").endswith(" Family Trust")
    assert m.replace("ORGANIZATION", "WATERLOO INDUSTRIAL PARK VI PRIVATE LIMITED").endswith(" PRIVATE LIMITED")
    assert m.replace("ORGANIZATION", "Pandya Solutions LIMITED").endswith(" Limited")


def test_email_mirrors_fake_person():
    m = ReplacementMap(seed=1)
    fake = m.replace("PERSON", "Sarthak Malvadkar")
    email = m.replace("EMAIL_ADDRESS", "sarthak.malvadkar@kshinternational.com")
    first, last = fake.casefold().split()
    assert email.startswith(f"{first}.{last}@") and email.endswith(".example.com")
    assert m.replace("URL", "www.kshinternational.com") == "www." + email.split("@")[1]


def test_phone_is_format_preserving():
    m = ReplacementMap(seed=1)
    fake = m.replace("PHONE_NUMBER", "+91 22 4009 4400")
    assert re.fullmatch(r"\+91 \d\d \d{4} \d{4}", fake) and fake != "+91 22 4009 4400"
    assert m.replace("PHONE_NUMBER", "+91 2240094400") == m.replace("PHONE_NUMBER", "+91 22 4009 4400")


def test_identifiers_are_deliberately_invalid():
    m = ReplacementMap(seed=3)
    for card in ("4111 1111 1111 1111", "5500 0000 0000 0004"):
        fake = m.replace("CREDIT_CARD", card)
        assert len(fake) == len(card) and not _luhn_valid(fake)
    aadhaar = m.replace("IN_AADHAAR", "2345 6789 0123")
    assert not verhoeff_valid(aadhaar)
    assert m.replace("US_SSN", "536-22-8741").startswith("9")
    assert re.fullmatch(r"(192\.0\.2|198\.51\.100|203\.0\.113)\.\d+", m.replace("IP_ADDRESS", "10.24.8.113"))
    assert re.fullmatch(r"[A-Z]{3}P[A-Z]\d{4}[A-Z]", m.replace("IN_PAN", "ABCPE1234F"))


def test_state_only_address_stays_short():
    fake = ReplacementMap(seed=1).replace("ADDRESS", "Maharashtra, India")
    assert fake.endswith(", India") and "," not in fake[: -len(", India")]


def test_fakes_are_unique():
    m = ReplacementMap(seed=1)
    fakes = {m.replace("PERSON", f"Person Number{i}") for i in range(200)}
    assert len(fakes) == 200
