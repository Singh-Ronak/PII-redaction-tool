"""Central configuration: which entities are redacted, thresholds, and the
allow-lists used by the post-analysis verification pass.

Adding a new PII type usually means: (1) add a recognizer in
``recognizers.py``, (2) list its entity name in ``TEXT_ENTITIES`` (and a
threshold if the default does not fit), (3) add a fake generator in
``faker_map.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Entities requested from Presidio for text. DATE_TIME and LOCATION are
# intentionally absent: generic dates and standalone city names are not PII in
# a prospectus; dates of birth and full addresses have dedicated recognizers.
TEXT_ENTITIES: tuple[str, ...] = (
    "PERSON",
    "ORGANIZATION",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "ADDRESS",
    "US_SSN",
    "CREDIT_CARD",
    "DATE_OF_BIRTH",
    "IP_ADDRESS",
    "URL",
    "IN_PAN",
    "IN_AADHAAR",
    "IN_DIN",
)

# Rule-based recognizers emit internal entity names so that Presidio's
# de-duplication does not let a sloppy, longer spaCy span of the same type
# swallow a precise rule-based span. They are mapped back after analysis.
INTERNAL_ENTITY_ALIASES: dict[str, str] = {
    "ORG_SUFFIX": "ORGANIZATION",
    "PERSON_LABELLED": "PERSON",
}

DEFAULT_SCORE_THRESHOLD = 0.5

# Context-dependent recognizers emit a low base score that only crosses the
# threshold when Presidio's context enhancer finds a supporting word.
ENTITY_THRESHOLDS: dict[str, float] = {
    "DATE_OF_BIRTH": 0.4,
    "IN_DIN": 0.4,
    "IN_AADHAAR": 0.4,
    "IN_PAN": 0.5,
    "PHONE_NUMBER": 0.4,
    "US_SSN": 0.5,
    "URL": 0.5,
}

# Role titles / section headers that NER models frequently tag as PERSON or
# ORGANIZATION. Compared case-insensitively against the whole span.
ROLE_AND_HEADER_TERMS: frozenset[str] = frozenset(
    t.casefold()
    for t in (
        "Company Secretary", "Compliance Officer", "Company Secretary and Compliance Officer",
        "Director", "Directors", "Managing Director", "Joint Managing Director",
        "Executive Director", "Independent Director", "Whole-time Director",
        "Non-Executive Director", "Chairman", "Chairman and Executive Director",
        "Chief Executive Officer", "Chief Financial Officer", "CEO", "CFO", "KMP", "KMPs",
        "Technical Director", "Board", "Board of Directors", "Promoter", "Promoters",
        "Promoter Group", "Promoter Selling Shareholder", "Promoter Selling Shareholders",
        "Selling Shareholders", "Shareholders", "Bidder", "Bidders", "Registrar",
        "Registrar to the Offer", "Book Running Lead Managers", "BRLM", "BRLMs",
        "Syndicate", "Syndicate Members", "Underwriters", "Statutory Auditors", "Auditors",
        "Contact Person", "Investor Grievances", "Senior Management", "Key Managerial Personnel",
        "Anchor Investor", "Anchor Investors", "Retail Individual Investors", "RIIs", "QIBs",
        "NIIs", "Mutual Funds", "Designated Intermediaries", "Sponsor Banks", "Sponsor Bank",
        "Escrow Collection Bank", "Public Offer Account Bank", "Refund Bank", "Bankers to the Offer",
        "Bankers to our Company", "Self-Certified Syndicate Banks", "SCSB", "SCSBs",
        "Legal Counsel", "Monitoring Agency", "Our Company", "the Company", "Company", "Issuer",
        "Red Herring Prospectus", "Draft Red Herring Prospectus", "Prospectus", "Offer",
        "Fresh Issue", "Offer for Sale", "Equity Shares", "Net Proceeds", "Gross Proceeds",
        "Parents Branch", "Family Branch", "Family Branches", "Promoter Trusts", "Group Companies",
        "Group Entities", "Corporate Promoter", "Independent Chartered Engineer",
        "Industry Data Provider", "CARE Report", "Materiality Policy", "IPO Committee",
    )
)

# Public authorities, regulators and market infrastructure. These are not
# private companies/persons, so they are kept (explicit precision choice).
PUBLIC_BODIES: frozenset[str] = frozenset(
    t.casefold()
    for t in (
        "SEBI", "Securities and Exchange Board of India", "BSE", "BSE Limited", "NSE",
        "National Stock Exchange of India Limited", "National Stock Exchange of India",
        "Stock Exchanges", "Stock Exchange", "RoC", "Registrar of Companies",
        "Registrar of Companies, Maharashtra at Pune", "Registrar of Companies, Maharashtra at Mumbai",
        "Registrar of Companies, Maharashtra at Bombay", "RBI", "Reserve Bank of India",
        "Government of India", "GoI", "Government of Maharashtra", "Income Tax Department",
        "UIDAI", "Unique Identification Authority of India", "ICAI",
        "Institute of Chartered Accountants of India", "NSDL", "CDSL", "Ministry of Finance",
        "Ministry of Corporate Affairs", "MCA", "NPCI", "Central Processing Centre",
        "Registrar of Companies, Central Processing Centre", "Govt. of India", "GOVT. OF INDIA",
        "Income Tax PAN Services Unit", "Supreme Court", "High Court",
    )
)

# If any of these words appears in an ORGANIZATION/PERSON span it is a legal
# instrument, defined term or document section rather than an entity name.
NON_ENTITY_TOKENS: frozenset[str] = frozenset(
    t.casefold()
    for t in (
        "Act", "Acts", "Regulation", "Regulations", "Rules", "Rule", "Circular", "Policy",
        "Scheme", "Code", "Committee", "Offer", "Prospectus", "Shares", "Share", "Portion",
        "Investors", "Investor", "Bid", "Bids", "Bidding", "ASBA", "UPI", "Fiscal", "Fiscals",
        "Section", "Chapter", "Schedule", "Annexure", "Note", "Notes", "Form", "Forms",
        "Mechanism", "Process", "Agreement", "Report", "Statements", "Statement", "Standards",
        "Ind", "AS", "GAAP", "Page", "Pages", "Risk", "Risks", "Factors", "Summary", "Price",
        "Band", "Period", "Date", "Day", "Days", "Capital", "Proceeds", "Objects", "Issue",
        "Allotment", "Details", "Particulars", "Total", "Sub-total", "Branch", "Branches",
        "Director", "Directors", "Secretary", "Officer", "Promoter", "Promoters", "Shareholder",
        "Shareholders", "Registrar", "Managers", "Management", "Auditors", "Personnel",
        "Engineer", "Counsel", "Unit", "Facility", "Office", "Department", "Division",
    )
)

# Words that are legal-entity suffixes / org keywords; any span carrying one of
# these is treated as an ORGANIZATION rather than a PERSON.
ORG_KEYWORDS: frozenset[str] = frozenset(
    t.casefold()
    for t in (
        "Limited", "Ltd", "Ltd.", "LLP", "Pvt", "Pvt.", "Private", "Bank", "Trust", "Foundation",
        "Associates", "Securities", "Corporation", "Inc", "Inc.", "Co.", "N.A.", "Fund",
        "Industries", "Motors", "Electricals", "Ventures", "Holdings", "Capital", "Finance",
        "Wealth", "Analytics", "Advisory", "Ratings", "Distriparks", "Logistics", "Citibank",
        "AB", "GmbH", "AG", "Pte", "Corp", "Corp.", "Co", "LLC", "PLC",
    )
)

# URLs on these domains belong to regulators/public infrastructure and are kept.
PUBLIC_URL_DOMAINS: tuple[str, ...] = (
    "sebi.gov.in", "bseindia.com", "nseindia.com", "gov.in", "nic.in", "rbi.org.in",
)

INDIAN_STATES: tuple[str, ...] = (
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat",
    "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh",
    "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab",
    "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh",
    "Uttarakhand", "West Bengal", "Delhi", "New Delhi", "Jammu and Kashmir", "Ladakh",
    "Puducherry", "Chandigarh",
)


@dataclass
class RedactionConfig:
    """Runtime options for a redaction run."""

    seed: int = 42
    spacy_model: str = "en_core_web_lg"
    faker_locale: str = "en_IN"
    redact_images: bool = True
    redact_faces: bool = True
    redact_qr_codes: bool = True
    ocr_lang: str = "auto"  # "auto" -> "eng+hin" when Hindi data is installed, else "eng"
    tesseract_cmd: str | None = None
    ocr_min_confidence: float = 30.0
    box_padding: int = 4
    entities: tuple[str, ...] = TEXT_ENTITIES + tuple(INTERNAL_ENTITY_ALIASES)
    thresholds: dict[str, float] = field(default_factory=lambda: dict(ENTITY_THRESHOLDS))
    default_threshold: float = DEFAULT_SCORE_THRESHOLD
    mapping_path: Path | None = None  # original->fake mapping is itself sensitive; opt-in only

    def threshold_for(self, entity_type: str) -> float:
        entity_type = INTERNAL_ENTITY_ALIASES.get(entity_type, entity_type)
        return self.thresholds.get(entity_type, self.default_threshold)
