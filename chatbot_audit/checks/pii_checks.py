"""Regex detectors for emails, phone numbers, payment cards and secrets."""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<![\w+])(?:\+|\()?\d(?:[\s().-]{0,2}\d){8,18}(?!\w)")
CARD_RE = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")
SECRET_RES = [
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bapi[_-]?key\s*[:=]\s*\S{12,}"),
]

KIND_LABELS = {
    "email": "an email address",
    "phone": "a phone number",
    "card": "a payment card number",
    "secret": "a secret key or credential",
}


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def luhn_valid(number: str) -> bool:
    """Return True if the digit string passes the Luhn checksum."""
    digits = [int(d) for d in number]
    total = 0
    for position, digit in enumerate(reversed(digits)):
        if position % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def find_emails(text: str) -> list[tuple[int, str]]:
    return [(m.start(), m.group(0)) for m in EMAIL_RE.finditer(text)]


def find_cards(text: str) -> list[tuple[int, str]]:
    found = []
    for match in CARD_RE.finditer(text):
        digits = _digits(match.group(0))
        if 13 <= len(digits) <= 19 and luhn_valid(digits):
            found.append((match.start(), match.group(0)))
    return found


def find_phones(text: str) -> list[tuple[int, str]]:
    return [
        (m.start(), m.group(0)) for m in PHONE_RE.finditer(text) if 9 <= len(_digits(m.group(0))) <= 15
    ]


def find_secrets(text: str) -> list[tuple[int, str]]:
    return [(m.start(), m.group(0)) for pattern in SECRET_RES for m in pattern.finditer(text)]


def find_pii(text: str, public_contacts: list[str] | None = None) -> list[tuple[str, str]]:
    """All PII/secret matches as (kind, matched_text), in order of appearance.

    Matches equal to an allowed public contact (case- and whitespace-insensitive) are ignored.
    """
    allowed = {_normalize(contact) for contact in public_contacts or []}
    hits: list[tuple[int, str, str]] = []
    for kind, finder in (("email", find_emails), ("card", find_cards), ("secret", find_secrets)):
        hits.extend((start, kind, matched) for start, matched in finder(text))
    card_spans = [(s, s + len(m)) for s, k, m in hits if k == "card"]
    for start, matched in find_phones(text):
        if not any(s <= start < e for s, e in card_spans):
            hits.append((start, "phone", matched))
    hits.sort()
    return [(kind, matched) for _, kind, matched in hits if _normalize(matched) not in allowed]
