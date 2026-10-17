from chatbot_audit.checks.pii_checks import find_pii, luhn_valid


def kinds(text: str, public: list[str] | None = None) -> list[str]:
    return [kind for kind, _ in find_pii(text, public)]


def test_detects_email() -> None:
    assert find_pii("Contact jane.doe@example.com today") == [("email", "jane.doe@example.com")]


def test_detects_phone_formats() -> None:
    assert kinds("Call +44 7700 900123 now") == ["phone"]
    assert kinds("Call (555) 123-4567 now") == ["phone"]
    assert kinds("Call 555.123.4567") == ["phone"]


def test_short_numbers_are_not_phones() -> None:
    assert find_pii("Order 1002 costs 50 EUR, 30% off, ref 2026-10-17") == []


def test_detects_luhn_valid_card() -> None:
    assert luhn_valid("4111111111111111")
    assert find_pii("Card 4111 1111 1111 1111 on file") == [("card", "4111 1111 1111 1111")]
    assert kinds("Card 4111-1111-1111-1111") == ["card"]


def test_rejects_luhn_invalid_card() -> None:
    assert not luhn_valid("4111111111111112")
    assert find_pii("Number 4111 1111 1111 1112") == []


def test_ignores_public_contacts() -> None:
    public = ["support@lumen-skincare.example", "+44 20 7946 0000"]
    assert find_pii("Email Support@Lumen-Skincare.example or call +44 20 7946 0000", public) == []
    assert kinds("Email support@lumen-skincare.example or jane@example.com", public) == ["email"]


def test_detects_secrets() -> None:
    assert kinds("key sk-abcdefghijklmnopqrstuvwx123") == ["secret"]
    assert kinds("AKIAABCDEFGHIJKLMNOP is the id") == ["secret"]
    assert kinds("api_key = abcdef123456789") == ["secret"]


def test_results_are_in_order_of_appearance() -> None:
    text = "Order 1002 belongs to jane.doe@example.com, phone +44 7700 900123."
    assert find_pii(text) == [("email", "jane.doe@example.com"), ("phone", "+44 7700 900123")]


# Phase 1.1 fix 3: fewer phone false positives.
def test_order_numbers_are_not_phones() -> None:
    assert find_pii("Your order #123456789 has shipped.") == []
    assert find_pii("Tracking number 1234 5678 9012 is on its way.") == []
    assert find_pii("See invoice ref 123-456-7890 for details.") == []


def test_iso_dates_and_datetimes_are_not_phones() -> None:
    assert find_pii("Delivered 2026-10-17 14:30") == []
    assert find_pii("Updated 2026-10-17T14:30:05Z and 2026-10-18") == []


def test_ungrouped_digits_without_phone_words_are_not_phones() -> None:
    assert find_pii("Your account number is 123456789.") == []


def test_real_phones_still_detected() -> None:
    assert find_pii("Call +44 7700 900123") == [("phone", "+44 7700 900123")]
    assert find_pii("phone: 01711-234567") == [("phone", "01711-234567")]
    assert find_pii("Reach me on WhatsApp 01711234567") == [("phone", "01711234567")]
