"""Consistent, unique fake replacements (Faker ``en_IN``).

Every original value is normalised to a key (case, whitespace and phone
formatting are ignored), so "Kushal Subbayya Hegde", "KUSHAL SUBBAYYA HEGDE"
and "Kushal  Subbayya\tHegde" all map to the same fake person, rendered in the
original's letter case. Generated values are never reused for a different
original.

Identifier-style values (phone, Aadhaar, PAN, card, SSN, IP) are generated
format-preserving so the document layout does not shift. Numbers that could
belong to a real person are deliberately made invalid (Aadhaar fails the
Verhoeff check, cards fail Luhn, IPs come from the RFC 5737 documentation
ranges, SSNs use the never-issued 9xx area).
"""

from __future__ import annotations

import random
import re
import string
from datetime import date
from typing import Callable

from faker import Faker

from .config import INDIAN_STATES
from .recognizers import verhoeff_valid

_ORG_SUFFIX_RE = re.compile(
    r"(?:,?\s+(?:Private\s+Limited|Pvt\.?\s+Ltd\.?|Limited|Ltd\.?|LLP|L\.L\.P\.|Family\s+Trust|Trust|Foundation|"
    r"Bank(?:\s+of\s+India)?|Associates|N\.A\.|Securities))+$",
    re.IGNORECASE,
)
_ORG_DESCRIPTORS = [
    "Industries", "Enterprises", "Holdings", "Ventures", "Traders", "Infratech", "Engineering",
    "Consultants", "Technologies", "Exports", "Solutions", "Commercial",
]


_STATE_ONLY_RE = re.compile(
    "(?i)\\(?(?:" + "|".join(re.escape(s) for s in INDIAN_STATES) + ")\\)?,?\\s*(?:India)?\\.?"
)
_UPPER_SUFFIX_WORDS = {"llp", "l.l.p.", "n.a."}


def _canonical_suffix(suffix: str) -> str:
    """'LIMITED' / 'limited' -> 'Limited', 'llp' -> 'LLP'; match_case re-applies all-caps originals."""
    return " ".join(w.upper() if w.casefold() in _UPPER_SUFFIX_WORDS else
                    w if w.casefold() == "of" else w[:1].upper() + w[1:].lower()
                    for w in suffix.split())


def strip_org_suffix(name: str) -> str:
    """'Waterloo Motors Private Limited' -> 'Waterloo Motors'."""
    return _ORG_SUFFIX_RE.sub("", name).strip(" ,")


def _normalise(entity_type: str, value: str) -> str:
    v = re.sub(r"\s+", " ", value).strip().casefold()
    if entity_type in ("PHONE_NUMBER",):
        digits = re.sub(r"\D", "", v)
        return digits[-10:] if len(digits) >= 10 else digits
    if entity_type in ("IN_AADHAAR", "CREDIT_CARD", "US_SSN", "IN_DIN"):
        return re.sub(r"\D", "", v)
    if entity_type == "URL":
        v = re.sub(r"^[a-z]+://", "", v)
        return re.sub(r"^www\.", "", v).rstrip("/")
    return re.sub(r"[\s,.;:]+$", "", v)


def match_case(original: str, fake: str) -> str:
    letters = [c for c in original if c.isalpha()]
    if letters and all(c.isupper() for c in letters) and len(letters) > 1:
        return fake.upper()
    if letters and all(c.islower() for c in letters):
        return fake.lower()
    return fake


class ReplacementMap:
    """Maps (entity type, original value) -> fake value, consistently and uniquely."""

    def __init__(self, locale: str = "en_IN", seed: int = 42):
        self.fake = Faker(locale)
        self.fake.seed_instance(seed)
        self.rng = random.Random(seed)
        self._map: dict[tuple[str, str], str] = {}
        self._used: set[str] = set()
        self._person_tokens: dict[str, str] = {}
        self._domains: dict[str, str] = {}
        self._generators: dict[str, Callable[[str], str]] = {
            "PERSON": self._person,
            "ORGANIZATION": self._organization,
            "EMAIL_ADDRESS": self._email,
            "PHONE_NUMBER": self._phone,
            "ADDRESS": self._address,
            "US_SSN": self._ssn,
            "CREDIT_CARD": self._credit_card,
            "DATE_OF_BIRTH": self._dob,
            "IP_ADDRESS": self._ip,
            "URL": self._url,
            "IN_PAN": self._pan,
            "IN_AADHAAR": self._aadhaar,
            "IN_DIN": self._din,
        }

    # ------------------------------------------------------------------ API
    def replace(self, entity_type: str, original: str) -> str:
        if entity_type == "URL":
            # Domains are mapped consistently; scheme/"www." follow each original occurrence.
            return self._url(original)
        key = (entity_type, _normalise(entity_type, original))
        if key not in self._map:
            generator = self._generators.get(entity_type, self._generic)
            self._map[key] = self._unique(lambda: generator(original))
        return match_case(original, self._map[key]) if entity_type in (
            "PERSON", "ORGANIZATION", "ADDRESS"
        ) else self._map[key]

    def mapping(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for (etype, key), fake in self._map.items():
            out.setdefault(etype, {})[key] = fake
        if self._domains:
            out["DOMAIN"] = dict(self._domains)
        return out

    def __len__(self) -> int:
        return len(self._map)

    # ------------------------------------------------------------ internals
    def _unique(self, make: Callable[[], str]) -> str:
        for _ in range(50):
            value = make()
            if value.casefold() not in self._used:
                self._used.add(value.casefold())
                return value
        value = f"{make()} {len(self._used)}"
        self._used.add(value.casefold())
        return value

    def _digits(self, template: str, keep_prefix: int = 0) -> str:
        """Replace every digit after the first ``keep_prefix`` digits, keeping separators."""
        out, seen = [], 0
        for c in template:
            if c.isdigit():
                out.append(c if seen < keep_prefix else str(self.rng.randint(0, 9)))
                seen += 1
            else:
                out.append(c)
        return "".join(out)

    def _person(self, original: str) -> str:
        tokens = original.split()
        n = max(1, min(len(tokens), 4))
        if n == 1:
            parts = [self.fake.first_name()]
        else:
            parts = [self.fake.first_name()] + [self.fake.first_name() for _ in range(n - 2)] + [self.fake.last_name()]
        for o, f in zip(tokens, parts):
            self._person_tokens.setdefault(o.casefold().strip("."), f)
        return " ".join(parts)

    def _organization(self, original: str) -> str:
        base = f"{self.fake.last_name()} {self.rng.choice(_ORG_DESCRIPTORS)}"
        m = _ORG_SUFFIX_RE.search(original)
        if not m:
            return base
        suffix = original[m.start():]
        if re.search(r"(?i)\b(?:bank|trust|foundation|associates)\b", suffix):
            base = self.fake.last_name()  # "Sharma Family Trust", not "Sharma Holdings Family Trust"
        sep = ", " if suffix.startswith(",") else " "
        return f"{base}{sep}{_canonical_suffix(suffix.lstrip(', '))}"

    def _domain(self, domain: str) -> str:
        domain = domain.casefold()
        if domain not in self._domains:
            word = re.sub(r"[^a-z]", "", self.fake.last_name().casefold()) or "org"
            candidate = f"{word}.example.com"
            while candidate in self._domains.values():
                candidate = f"{word}{self.rng.randint(1, 99)}.example.com"
            self._domains[domain] = candidate
        return self._domains[domain]

    def _email(self, original: str) -> str:
        local, _, domain = original.partition("@")
        pieces = re.split(r"([._-])", local)
        mapped = []
        name_hit = False
        for p in pieces:
            core = p.rstrip(string.digits).casefold()
            if core and core in self._person_tokens:
                mapped.append(self._person_tokens[core].casefold() + p[len(core):])
                name_hit = True
            else:
                mapped.append(p)
        if name_hit:
            new_local = "".join(mapped)
        else:
            new_local = re.sub(r"[^a-z0-9._]", "", self.fake.user_name().casefold())
        return f"{new_local}@{self._domain(domain or 'example.com')}"

    def _url(self, original: str) -> str:
        original = re.sub(r"\.\s+", ".", original)  # "www.example. com" (conversion artefact)
        m = re.match(r"(?i)^(?P<scheme>[a-z]+://)?(?P<www>www\.)?(?P<host>[^/\s]+)", original)
        if not m:
            return f"www.{self._domain(original)}"
        host = m.group("host")
        return f"{m.group('scheme') or ''}{m.group('www') or ''}{self._domain(host)}"

    def _phone(self, original: str) -> str:
        digits = re.sub(r"\D", "", original)
        keep = 2 if original.strip().startswith("+") or digits.startswith("91") and len(digits) > 10 else 1
        return self._digits(original, keep_prefix=keep)

    def _address(self, original: str) -> str:
        if _STATE_ONLY_RE.fullmatch(original.strip()):
            return f"{self.fake.state()}, India"
        parts = [self.fake.street_address().replace("\n", ", ")]
        if re.search(r"\d{3}\s?\d{3}|\d{2}[lI]\s?\d{3}", original):
            parts.append(f"{self.fake.city()} – {self.fake.postcode()}")
        elif re.search(r"[A-Z][a-z]+", original) and len(original) > 40:
            parts.append(self.fake.city())
        if re.search(r"\bIndia\b", original, re.IGNORECASE):
            parts.append(f"{self.fake.state()}, India")
        return ", ".join(parts)

    def _ssn(self, original: str) -> str:
        fake = self._digits(original)
        return re.sub(r"^\d", "9", fake, count=1)

    def _credit_card(self, original: str) -> str:
        while True:
            fake = self._digits(original, keep_prefix=1)
            if not _luhn_valid(fake):
                return fake

    def _dob(self, original: str) -> str:
        d = self.fake.date_of_birth(minimum_age=21, maximum_age=80)
        return _format_like(original, d)

    def _ip(self, original: str) -> str:
        if ":" in original:
            return f"2001:db8::{self.rng.randint(1, 0xFFFF):x}"
        net = self.rng.choice(["192.0.2", "198.51.100", "203.0.113"])
        return f"{net}.{self.rng.randint(1, 254)}"

    def _pan(self, original: str) -> str:
        letters = string.ascii_uppercase
        holder_type = original[3].upper() if len(original) >= 4 and original[3].isalpha() else "P"
        return (
            "".join(self.rng.choice(letters) for _ in range(3)) + holder_type + self.rng.choice(letters)
            + "".join(str(self.rng.randint(0, 9)) for _ in range(4)) + self.rng.choice(letters)
        )

    def _aadhaar(self, original: str) -> str:
        while True:
            fake = self._digits(original)
            digits = re.sub(r"\D", "", fake)
            if digits[0] in "01":
                continue
            if not verhoeff_valid(digits):
                return fake

    def _din(self, original: str) -> str:
        return self._digits(original)

    def _generic(self, original: str) -> str:
        return "".join(
            str(self.rng.randint(0, 9)) if c.isdigit() else self.rng.choice(string.ascii_uppercase) if c.isalpha() else c
            for c in original
        )


def _luhn_valid(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _format_like(original: str, d: date) -> str:
    """Render ``d`` in the same shape as ``original`` (dd/mm/yyyy, 12 March 1988, ...)."""
    m = re.fullmatch(r"(\d{1,2})([/.-])(\d{1,2})\2(\d{4})", original.strip())
    if m:
        sep = m.group(2)
        return f"{d.day:02d}{sep}{d.month:02d}{sep}{d.year}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", original.strip()):
        return d.isoformat()
    if re.match(r"\d", original.strip()):
        return d.strftime("%d %B %Y")
    return d.strftime("%B %d, %Y")
