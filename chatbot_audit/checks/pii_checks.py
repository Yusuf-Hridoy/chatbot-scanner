"""Regex detectors for emails, phone numbers, payment cards and secrets."""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<![\w+])(?:\+|\()?\d(?:[\s().-]{0,2}\d){8,18}(?!\w)")
PHONE_GROUPED_RE = re.compile(r"\d[\s()-]+\d")  # digit groups separated by spaces, dashes or brackets
ISO_DATE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?(?!\d)"
)
REFERENCE_WORDS = {"order", "tracking", "invoice", "ref", "reference"}
PHONE_WORDS = {"call", "phone", "tel", "mobile", "whatsapp"}
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


def _words_before(text: str, position: int, count: int) -> set[str]:
    return set(re.findall(r"[a-z]+", text[:position].lower())[-count:])


def find_phones(text: str) -> list[tuple[int, str]]:
    """Phone numbers with 9-15 digits that look like phones, not references or dates.

    Ignored: ISO dates/date-times, numbers after "#" or within 3 words after
    order/tracking/invoice/ref/reference. A match needs a leading "+", grouped digits
    (spaces, dashes or brackets), or call/phone/tel/mobile/whatsapp within the 5 words before it.
    """
    # Blank out dates with a word character so they can neither match nor join neighbouring digits.
    masked = ISO_DATE_RE.sub(lambda m: "_" * len(m.group(0)), text)
    found = []
    for match in PHONE_RE.finditer(masked):
        candidate = match.group(0)
        if not 9 <= len(_digits(candidate)) <= 15:
            continue
        if masked[: match.start()].rstrip().endswith("#"):
            continue
        if REFERENCE_WORDS & _words_before(masked, match.start(), 3):
            continue
        looks_like_phone = (
            candidate.startswith(("+", "("))
            or PHONE_GROUPED_RE.search(candidate) is not None
            or bool(PHONE_WORDS & _words_before(masked, match.start(), 5))
        )
        if looks_like_phone:
            found.append((match.start(), text[match.start() : match.end()]))
    return found


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
