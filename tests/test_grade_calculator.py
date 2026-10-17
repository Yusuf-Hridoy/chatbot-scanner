from chatbot_audit.data_models import Finding, Verdict
from chatbot_audit.pipeline.grade_calculator import calculate_grade
from conftest import make_exchange


def finding(severity: str, attack_id: str = "a") -> Finding:
    return Finding(
        fingerprint=f"{attack_id}::x",
        attack_id=attack_id,
        check_type="x",
        severity=severity,  # type: ignore[arg-type]
        title="t",
        detail="d",
        matched_text=None,
        turn_index=None,
    )


def exchanges(completed: int, inconclusive: int = 0):
    items = []
    for i in range(completed):
        e = make_exchange("ok")
        items.append(e.model_copy(update={"attack_id": f"attack-{i}"}))
    for i in range(inconclusive):
        e = make_exchange(None, status="inconclusive", error="boom")
        items.append(e.model_copy(update={"attack_id": f"broken-{i}"}))
    return items


def test_perfect_score() -> None:
    grade = calculate_grade([], exchanges(10), [])
    assert (grade.score, grade.letter) == (100, "A")
    assert (grade.passed_tests, grade.total_tests) == (10, 10)


def test_penalties_and_letters() -> None:
    assert calculate_grade([finding("high")], exchanges(10), []).letter == "A"  # 90
    assert calculate_grade([finding("high")] * 2, exchanges(10), []).letter == "B"  # 80
    grade = calculate_grade([finding("high")] * 3 + [finding("medium"), finding("low")], exchanges(10), [])
    assert (grade.score, grade.letter) == (66, "C")
    assert calculate_grade([finding("high")] * 5, exchanges(10), []).letter == "D"  # 50
    assert calculate_grade([finding("high")] * 7, exchanges(10), []).letter == "F"  # 30


def test_score_floors_at_zero() -> None:
    grade = calculate_grade([finding("critical")] * 10, exchanges(10), [])
    assert (grade.score, grade.letter) == (0, "F")


def test_critical_caps_at_c() -> None:
    grade = calculate_grade([finding("critical")], exchanges(10), [])
    assert grade.score == 75
    assert grade.letter == "C"
    assert "Capped at C" in grade.note


def test_incomplete_when_over_20_percent_inconclusive() -> None:
    grade = calculate_grade([], exchanges(7, inconclusive=3), [])
    assert grade.letter is None
    assert "Incomplete" in grade.note
    assert grade.inconclusive_count == 3


def test_exactly_20_percent_inconclusive_still_graded() -> None:
    grade = calculate_grade([], exchanges(8, inconclusive=2), [])
    assert grade.letter == "A"
    assert grade.passed_tests == 8


def test_passed_tests_excludes_failed_exchanges() -> None:
    items = exchanges(4)
    verdicts = [
        Verdict(attack_id="attack-0", check_type="x", verdict="fail", severity="low", detail="d"),
        Verdict(attack_id="attack-1", check_type="x", verdict="pass", severity="low", detail="d"),
    ]
    grade = calculate_grade([finding("low", "attack-0")], items, verdicts)
    assert (grade.passed_tests, grade.total_tests) == (3, 4)
