"""Full pipeline through the demo website's chat widget in a real browser (needs Chromium)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from chatbot_audit.data_models import Finding, Verdict
from chatbot_audit.pipeline.grade_calculator import calculate_grade
from chatbot_audit.pipeline.run_folder import REPORT_HTML_FILE, VERDICTS_FILE, read_jsonl, read_latest_exchanges
from chatbot_audit.pipeline.step1_run_attacks import run_attacks
from chatbot_audit.pipeline.step2_evaluate_results import evaluate_run
from chatbot_audit.pipeline.step4_build_report import build_report

pytestmark = pytest.mark.browser


def quiet(_: str) -> None:
    pass


def run_full(config_path: Path, tier: str | None = None) -> tuple[Path, list[Finding]]:
    run_dir = run_attacks(config_path, tier=tier, echo=quiet)
    findings = evaluate_run(run_dir, echo=quiet)
    build_report(run_dir, echo=quiet)
    return run_dir, findings


def grade_of(run_dir: Path, findings: list[Finding]):
    return calculate_grade(findings, read_latest_exchanges(run_dir), read_jsonl(run_dir / VERDICTS_FILE, Verdict))


def check_unsafe_browser_matches_http(unsafe_bot, make_config, tier: str | None, attack_count: int) -> None:
    http_dir, http_findings = run_full(make_config(unsafe_bot, kind="http"), tier)
    browser_dir, browser_findings = run_full(make_config(unsafe_bot, kind="browser"), tier)

    assert sorted(f.attack_id for f in browser_findings) == sorted(f.attack_id for f in http_findings)
    http_grade, browser_grade = grade_of(http_dir, http_findings), grade_of(browser_dir, browser_findings)
    assert (browser_grade.letter, browser_grade.score) == (http_grade.letter, http_grade.score) == ("F", 0)
    assert browser_grade.counts_by_severity == http_grade.counts_by_severity

    exchanges = read_latest_exchanges(browser_dir)
    assert len(exchanges) == attack_count
    for exchange in exchanges:
        assert exchange.status == "completed", exchange.error
        assert exchange.adapter_type == "browser"
        assert exchange.evidence_screenshot == f"evidence/{exchange.attack_id}.png"
        assert exchange.evidence_video == f"evidence/{exchange.attack_id}.webm"
        assert (browser_dir / exchange.evidence_screenshot).stat().st_size > 0
        assert (browser_dir / exchange.evidence_video).stat().st_size > 0

    html = (browser_dir / REPORT_HTML_FILE).read_text(encoding="utf-8")
    images = re.findall(r'<img class="evidence-shot" src="(evidence/[^"]+\.png)"', html)
    assert len(images) == len(browser_findings)
    assert html.count("Watch the recording") == len(browser_findings)
    assert "real browser with a fresh session" in html


def check_safe_browser_run_is_clean(safe_bot, make_config, tier: str | None) -> None:
    run_dir, findings = run_full(make_config(safe_bot, kind="browser"), tier)
    assert findings == []
    grade = grade_of(run_dir, findings)
    assert (grade.letter, grade.score, grade.inconclusive_count) == ("A", 100, 0)
    assert "No issues found in this run." in (run_dir / REPORT_HTML_FILE).read_text(encoding="utf-8")


# Quick tier (12 attacks): part of the fast test run.


def test_unsafe_browser_quick_run_matches_http_run(unsafe_bot, make_config) -> None:
    check_unsafe_browser_matches_http(unsafe_bot, make_config, "quick", 12)


def test_safe_browser_quick_run_is_clean(safe_bot, make_config) -> None:
    check_safe_browser_run_is_clean(safe_bot, make_config, "quick")


# Standard tier (all 30 attacks): slow, run before sharing changes.


@pytest.mark.slow
def test_unsafe_browser_run_matches_http_run(unsafe_bot, make_config) -> None:
    check_unsafe_browser_matches_http(unsafe_bot, make_config, None, 30)


@pytest.mark.slow
def test_safe_browser_run_is_clean(safe_bot, make_config) -> None:
    check_safe_browser_run_is_clean(safe_bot, make_config, None)
