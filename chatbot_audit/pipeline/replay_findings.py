"""`audit replay`: re-run the attacks that produced findings in a real browser, for video evidence."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from chatbot_audit.adapters.base_adapter import create_adapter
from chatbot_audit.attacks.attack_loader import load_attacks
from chatbot_audit.config_loader import ConfigError, load_client_config
from chatbot_audit.data_models import Finding, RunInfo
from chatbot_audit.pipeline.run_folder import (
    EVIDENCE_DIR,
    FINDINGS_FILE,
    REPLAYS_FILE,
    RUN_INFO_FILE,
    read_json,
    require_run_dir,
    resolve_config_path,
)
from chatbot_audit.pipeline.step1_run_attacks import apply_headed, execute_attacks, warn_if_unverified

NO_EVIDENCE_TARGET = "Add an evidence_target (a browser target) to the client config to record replays."
REPLAY_SUFFIX = ".replay"


async def replay_findings_async(
    run_dir: Path, echo: Callable[[str], None] = print, headed: bool = False
) -> Path:
    """Replay every attack with a finding through `evidence_target`; returns replays.jsonl.

    Replays are evidence only: verdicts and findings are never changed.
    """
    run_dir = require_run_dir(run_dir)
    info = RunInfo.model_validate(read_json(run_dir / RUN_INFO_FILE))
    config = load_client_config(resolve_config_path(run_dir, info))
    if config.evidence_target is None:
        raise ConfigError(NO_EVIDENCE_TARGET)
    apply_headed(config, headed)

    findings = [Finding.model_validate(item) for item in read_json(run_dir / FINDINGS_FILE)]
    attacks_by_id = {attack.id: attack for attack in load_attacks(Path(info.attack_pack_dir))}
    attacks = [attacks_by_id[f.attack_id] for f in findings if f.attack_id in attacks_by_id]

    replays_path = run_dir / REPLAYS_FILE
    replays_path.unlink(missing_ok=True)  # replays are re-recorded from scratch each time
    if not attacks:
        echo("No findings to replay.")
        replays_path.touch()
        return replays_path

    replay_config = config.model_copy(update={"target": config.evidence_target})
    warn_if_unverified(replay_config.target, echo)
    (run_dir / EVIDENCE_DIR).mkdir(exist_ok=True)
    echo(f"Replaying {len(attacks)} attack(s) with findings in a real browser...")
    await execute_attacks(
        attacks, create_adapter(replay_config), replay_config, run_dir, replays_path, REPLAY_SUFFIX, echo=echo
    )
    echo(f"Replays: {replays_path}")
    return replays_path


def replay_findings(run_dir: Path, echo: Callable[[str], None] = print, headed: bool = False) -> Path:
    """Synchronous wrapper around `replay_findings_async`."""
    return asyncio.run(replay_findings_async(run_dir, echo, headed))
