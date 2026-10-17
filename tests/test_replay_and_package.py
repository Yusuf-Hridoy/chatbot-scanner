"""`audit replay` and `audit package`."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from chatbot_audit.config_loader import ConfigError
from chatbot_audit.data_models import Exchange
from chatbot_audit.pipeline.package_run import package_run, referenced_evidence
from chatbot_audit.pipeline.replay_findings import NO_EVIDENCE_TARGET, replay_findings
from chatbot_audit.pipeline.run_folder import (
    FINDINGS_FILE,
    REPLAYS_FILE,
    REPORT_HTML_FILE,
    VERDICTS_FILE,
    read_jsonl,
)
from chatbot_audit.pipeline.step1_run_attacks import run_attacks
from chatbot_audit.pipeline.step2_evaluate_results import evaluate_run
from chatbot_audit.pipeline.step4_build_report import REPLAY_LABEL, build_report
from conftest import browser_target

FORBIDDEN_IN_ZIP = {"exchanges.jsonl", "verdicts.jsonl", "replays.jsonl", "run_info.json", "findings.json"}


def quiet(_: str) -> None:
    pass


def http_run(config_path: Path, tier: str = "quick") -> Path:
    run_dir = run_attacks(config_path, tier=tier, echo=quiet)
    evaluate_run(run_dir, echo=quiet)
    build_report(run_dir, echo=quiet)
    return run_dir


def test_replay_requires_evidence_target(unsafe_bot, make_config) -> None:
    run_dir = http_run(make_config(unsafe_bot))
    with pytest.raises(ConfigError) as excinfo:
        replay_findings(run_dir, echo=quiet)
    assert str(excinfo.value) == NO_EVIDENCE_TARGET


def test_package_http_run_without_evidence(unsafe_bot, make_config) -> None:
    run_dir = http_run(make_config(unsafe_bot))
    zip_path = package_run(run_dir, echo=quiet)
    assert zip_path.name == f"lumen-skincare-demo-{run_dir.name}-report.zip"
    with zipfile.ZipFile(zip_path) as archive:
        assert archive.namelist() == [REPORT_HTML_FILE]


def test_package_requires_a_report(unsafe_bot, make_config) -> None:
    run_dir = run_attacks(make_config(unsafe_bot), tier="quick", echo=quiet)
    with pytest.raises(ConfigError, match="audit report"):
        package_run(run_dir, echo=quiet)


def test_referenced_evidence_parsing() -> None:
    html = '<img src="evidence/a.png"><a href="evidence/a.webm">x</a><a href="report.pdf">p</a><img src="evidence/a.png">'
    assert referenced_evidence(html) == ["evidence/a.png", "evidence/a.webm"]


@pytest.mark.browser
def test_replay_then_package(unsafe_bot, make_config) -> None:
    config_path = make_config(unsafe_bot, evidence_target=browser_target(unsafe_bot))
    run_dir = http_run(config_path)
    findings_before = (run_dir / FINDINGS_FILE).read_bytes()
    verdicts_before = (run_dir / VERDICTS_FILE).read_bytes()
    finding_ids = sorted(item["attack_id"] for item in json.loads(findings_before))
    assert finding_ids  # the unsafe bot always has findings

    replay_findings(run_dir, echo=quiet)

    replays = read_jsonl(run_dir / REPLAYS_FILE, Exchange)
    assert sorted(r.attack_id for r in replays) == finding_ids
    for replay in replays:
        assert replay.adapter_type == "browser"
        assert replay.status == "completed", replay.error
        assert replay.evidence_screenshot == f"evidence/{replay.attack_id}.replay.png"
        assert replay.evidence_video == f"evidence/{replay.attack_id}.replay.webm"
        assert (run_dir / replay.evidence_screenshot).is_file()
        assert (run_dir / replay.evidence_video).is_file()
    assert (run_dir / FINDINGS_FILE).read_bytes() == findings_before
    assert (run_dir / VERDICTS_FILE).read_bytes() == verdicts_before

    build_report(run_dir, echo=quiet)
    html = (run_dir / REPORT_HTML_FILE).read_text(encoding="utf-8")
    assert html.count("Recorded in a real browser during a replay.") == len(finding_ids)
    assert "The bot&#39;s wording may differ slightly" in html or REPLAY_LABEL in html
    assert 'src="evidence/' in html

    zip_path = package_run(run_dir, echo=quiet)
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
    assert REPORT_HTML_FILE in names
    assert set(referenced_evidence(html)) <= names
    assert {f"evidence/{attack_id}.replay.png" for attack_id in finding_ids} <= names
    assert not names & FORBIDDEN_IN_ZIP
    assert not any(name.endswith((".yaml", ".yml")) for name in names)
