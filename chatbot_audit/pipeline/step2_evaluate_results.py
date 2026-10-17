"""Step 2: run every rule check over the recorded exchanges and build findings."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import Path

from chatbot_audit.attacks.attack_loader import load_attacks
from chatbot_audit.checks.rule_checks import run_check
from chatbot_audit.config_loader import load_client_config
from chatbot_audit.data_models import SEVERITY_ORDER, FailedCheck, Finding, RunInfo, Verdict
from chatbot_audit.pipeline.run_folder import (
    FINDINGS_FILE,
    RUN_INFO_FILE,
    VERDICTS_FILE,
    append_jsonl,
    read_json,
    read_latest_exchanges,
    require_run_dir,
    resolve_config_path,
    write_json,
)


def build_findings(verdicts: list[Verdict], titles: dict[str, str]) -> list[Finding]:
    """One auto-confirmed finding per attack that has failed checks.

    The finding lists every failed check and takes the highest severity among them.
    Sorted by severity (critical first), then attack id.
    """
    failed_by_attack: dict[str, list[FailedCheck]] = {}
    for verdict in verdicts:
        if verdict.verdict != "fail":
            continue
        failed_by_attack.setdefault(verdict.attack_id, []).append(
            FailedCheck(
                fingerprint=f"{verdict.attack_id}::{verdict.check_type}",
                check_type=verdict.check_type,
                severity=verdict.severity,
                detail=verdict.detail,
                matched_text=verdict.matched_text,
                turn_index=verdict.turn_index,
            )
        )
    findings = []
    for attack_id, checks in failed_by_attack.items():
        checks.sort(key=lambda c: (SEVERITY_ORDER[c.severity], c.check_type))
        findings.append(
            Finding(
                attack_id=attack_id,
                severity=checks[0].severity,
                title=titles.get(attack_id, attack_id),
                checks=checks,
                status="auto_confirmed",
            )
        )
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.attack_id))


def evaluate_run(run_dir: Path, echo: Callable[[str], None] = print) -> list[Finding]:
    """Evaluate a run folder: writes verdicts.jsonl and findings.json, prints a summary."""
    run_dir = require_run_dir(run_dir)
    info = RunInfo.model_validate(read_json(run_dir / RUN_INFO_FILE))
    config = load_client_config(resolve_config_path(run_dir, info))
    attacks = {attack.id: attack for attack in load_attacks(Path(info.attack_pack_dir))}
    exchanges = read_latest_exchanges(run_dir)

    verdicts: list[Verdict] = []
    for exchange in exchanges:
        attack = attacks.get(exchange.attack_id)
        if attack is None:
            echo(f"Warning: attack '{exchange.attack_id}' is no longer in the attack pack; skipping it.")
            continue
        verdicts.extend(run_check(check, exchange, config, attack) for check in attack.checks)

    verdicts_path = run_dir / VERDICTS_FILE
    verdicts_path.unlink(missing_ok=True)
    for verdict in verdicts:
        append_jsonl(verdicts_path, verdict)

    findings = build_findings(verdicts, {attack_id: attack.title for attack_id, attack in attacks.items()})
    write_json(run_dir / FINDINGS_FILE, findings)

    outcome_counts = Counter(verdict.verdict for verdict in verdicts)
    severity_counts = Counter(finding.severity for finding in findings)
    echo("")
    echo(f"Checks evaluated: {len(verdicts)} across {len(exchanges)} conversations")
    echo(f"  {'pass':<14}{outcome_counts['pass']:>4}")
    echo(f"  {'fail':<14}{outcome_counts['fail']:>4}")
    echo(f"  {'inconclusive':<14}{outcome_counts['inconclusive']:>4}")
    echo(f"  {'skipped':<14}{outcome_counts['skipped']:>4}")
    echo(f"Findings (one per attack): {len(findings)}")
    for severity in SEVERITY_ORDER:
        echo(f"  {severity:<14}{severity_counts[severity]:>4}")
    return findings
