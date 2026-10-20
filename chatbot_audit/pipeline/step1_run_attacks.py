"""Step 1: send every selected attack conversation to the chatbot and record the exchanges."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from chatbot_audit.adapters.base_adapter import AdapterError, BaseAdapter, ConversationEvidence, create_adapter
from chatbot_audit.adapters.widget_preset_loader import unverified_preset_warning
from chatbot_audit.attacks.attack_loader import DEFAULT_PACK_DIR, attack_pack_hash, filter_by_tier, load_attacks
from chatbot_audit.config_loader import ConfigError, load_client_config
from chatbot_audit.data_models import Attack, BrowserTargetConfig, ClientConfig, Exchange, RunInfo, TurnRecord
from chatbot_audit.pipeline.run_folder import (
    EVIDENCE_DIR,
    EXCHANGES_FILE,
    RUN_INFO_FILE,
    append_jsonl,
    create_run,
    locate_run,
    read_json,
    read_jsonl,
    relative_config_path,
    repair_jsonl,
    write_json,
)
from chatbot_audit.rate_limiter import RateLimiter

NOT_SENT = "Not sent because an earlier message in this conversation failed."
MAX_BROWSER_PARALLEL = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _send_with_retry(
    adapter: BaseAdapter, limiter: RateLimiter, config: ClientConfig, conversation_id: str, message: str
):
    """Send one message; retry exactly once after a pause on timeouts, connection errors and 5xx."""
    await limiter.wait()
    try:
        return await adapter.send(conversation_id, message)
    except AdapterError as exc:
        if not exc.retryable:
            raise
        await asyncio.sleep(config.limits.retry_pause_seconds)
        await limiter.wait()
        return await adapter.send(conversation_id, message)


def _relative(path: Path | None, run_dir: Path | None) -> str | None:
    if path is None or run_dir is None:
        return None
    return Path(path).resolve().relative_to(Path(run_dir).resolve()).as_posix()


async def run_conversation(
    attack: Attack,
    adapter: BaseAdapter,
    limiter: RateLimiter,
    config: ClientConfig,
    run_id: str,
    run_dir: Path | None = None,
    evidence_name: str | None = None,
    echo: Callable[[str], None] = print,
) -> Exchange:
    """Play one attack's turns in a single conversation and return the exchange.

    Evidence (screenshot/video, browser targets only) is saved to <run_dir>/evidence/<evidence_name>.*.
    Failing to save evidence only prints a warning; it never makes the exchange inconclusive.
    """
    started_at = _now()
    turns: list[TurnRecord] = []
    error: str | None = None
    conversation_id: str | None = None
    evidence = ConversationEvidence()
    try:
        conversation_id = await adapter.start_conversation()
        for index, message in enumerate(attack.turns):
            try:
                reply = await _send_with_retry(adapter, limiter, config, conversation_id, message)
            except AdapterError as exc:
                error = str(exc)
                turns.append(TurnRecord(index=index, sent=message, reply=None, latency_ms=None, error=error))
                break
            turns.append(
                TurnRecord(
                    index=index, sent=message, reply=reply.text, latency_ms=reply.latency_ms, buttons=reply.buttons
                )
            )
    except AdapterError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - a bug in one conversation must not stop the run
        error = f"Unexpected error: {exc.__class__.__name__}: {exc}"
    finally:
        if conversation_id is not None:
            evidence_dir = Path(run_dir) / EVIDENCE_DIR if run_dir is not None else None
            try:
                evidence = await adapter.end_conversation(conversation_id, evidence_dir, evidence_name or attack.id)
            except Exception as exc:  # noqa: BLE001 - evidence problems are never fatal
                echo(f"Warning: evidence for {attack.id} was not saved: {exc}")
            for warning in evidence.warnings:
                echo(f"Warning: {attack.id}: {warning}")
    for index in range(len(turns), len(attack.turns)):
        turns.append(TurnRecord(index=index, sent=attack.turns[index], reply=None, latency_ms=None, error=NOT_SENT))
    return Exchange(
        run_id=run_id,
        attack_id=attack.id,
        status="inconclusive" if error else "completed",
        turns=turns,
        error=error,
        started_at=started_at,
        finished_at=_now(),
        adapter_type=adapter.adapter_type,
        evidence_video=_relative(evidence.video_path, run_dir),
        evidence_screenshot=_relative(evidence.screenshot_path, run_dir),
    )


def effective_parallelism(config: ClientConfig, echo: Callable[[str], None] = print) -> int:
    """max_parallel_conversations, capped at 3 for browser targets."""
    wanted = config.limits.max_parallel_conversations
    if isinstance(config.target, BrowserTargetConfig) and wanted > MAX_BROWSER_PARALLEL:
        echo(f"Note: browser targets run at most {MAX_BROWSER_PARALLEL} conversations in parallel (config: {wanted}).")
        return MAX_BROWSER_PARALLEL
    return wanted


def apply_headed(config: ClientConfig, headed: bool) -> None:
    """--headed: show the browser window for debugging."""
    for target in (config.target, config.evidence_target):
        if headed and isinstance(target, BrowserTargetConfig):
            target.headless = False


def warn_if_unverified(target: object, echo: Callable[[str], None]) -> None:
    if isinstance(target, BrowserTargetConfig) and (warning := unverified_preset_warning(target)):
        echo(warning)


async def execute_attacks(
    attacks: list[Attack],
    adapter: BaseAdapter,
    config: ClientConfig,
    run_dir: Path,
    out_path: Path,
    evidence_suffix: str = "",
    total: int | None = None,
    done_before: int = 0,
    echo: Callable[[str], None] = print,
) -> None:
    """Run attacks concurrently, appending each finished exchange to `out_path` immediately."""
    total = total if total is not None else len(attacks)
    limiter = RateLimiter(config.limits.max_messages_per_minute)
    semaphore = asyncio.Semaphore(effective_parallelism(config, echo))
    counter = {"done": done_before}

    async def worker(attack: Attack) -> None:
        async with semaphore:
            exchange = await run_conversation(
                attack, adapter, limiter, config, run_dir.name, run_dir, f"{attack.id}{evidence_suffix}", echo
            )
        append_jsonl(out_path, exchange)
        counter["done"] += 1
        sent = sum(1 for turn in exchange.turns if turn.reply is not None)
        latency = sum(turn.latency_ms or 0 for turn in exchange.turns)
        echo(
            f"[{counter['done']}/{total}] {attack.id} ... {exchange.status} "
            f"({sent} turn{'s' if sent != 1 else ''}, {latency} ms)"
        )

    try:
        await asyncio.gather(*(worker(attack) for attack in attacks))
    finally:
        await adapter.close()


async def run_attacks_async(
    config_path: Path,
    tier: str | None = None,
    resume_run_id: str | None = None,
    attack_pack_dir: Path | None = None,
    echo: Callable[[str], None] = print,
    headed: bool = False,
) -> Path:
    """Run (or resume) all attacks for a config and return the run folder."""
    config_path = Path(config_path).resolve()
    config = load_client_config(config_path)
    apply_headed(config, headed)

    if resume_run_id:
        run_dir = locate_run(config_path, resume_run_id)
        if (run_dir / RUN_INFO_FILE).exists():
            info = RunInfo.model_validate(read_json(run_dir / RUN_INFO_FILE))
            if tier and tier != info.tier:
                echo(f"Note: resuming with the run's original tier '{info.tier}' (ignoring --tier {tier}).")
            tier = info.tier
            attack_pack_dir = attack_pack_dir or Path(info.attack_pack_dir)
    else:
        run_dir = create_run(config_path, config, tier)

    tier = tier or config.tier
    if tier not in ("quick", "standard", "deep"):
        raise ConfigError(f"Unknown tier '{tier}'. Use quick, standard or deep.")
    pack_dir = Path(attack_pack_dir or DEFAULT_PACK_DIR).resolve()
    attacks = filter_by_tier(load_attacks(pack_dir), tier)

    exchanges_path = run_dir / EXCHANGES_FILE
    repair_jsonl(exchanges_path)
    # Only completed exchanges count as done; inconclusive attacks are re-sent and appended.
    done_ids = {e.attack_id for e in read_jsonl(exchanges_path, Exchange) if e.status == "completed"}
    pending = [attack for attack in attacks if attack.id not in done_ids]
    run_id = run_dir.name

    previous = (
        RunInfo.model_validate(read_json(run_dir / RUN_INFO_FILE)) if (run_dir / RUN_INFO_FILE).exists() else None
    )
    info = RunInfo(
        run_id=run_id,
        client=config.client,
        display_name=config.display_name,
        tier=tier,
        adapter_type=config.target.type,
        attack_count=len(attacks),
        attack_pack_hash=attack_pack_hash(pack_dir),
        started_at=previous.started_at if previous else _now(),
        config_path=relative_config_path(config_path, run_dir),
        attack_pack_dir=str(pack_dir),
    )
    write_json(run_dir / RUN_INFO_FILE, info)

    echo(f"Run folder: {run_dir}")
    if done_ids:
        echo(f"Resuming: {len(attacks) - len(pending)} of {len(attacks)} attacks already done.")
    warn_if_unverified(config.target, echo)

    (run_dir / EVIDENCE_DIR).mkdir(exist_ok=True)
    adapter = create_adapter(config)
    await execute_attacks(
        pending,
        adapter,
        config,
        run_dir,
        exchanges_path,
        total=len(attacks),
        done_before=len(attacks) - len(pending),
        echo=echo,
    )

    info.finished_at = _now()
    write_json(run_dir / RUN_INFO_FILE, info)
    return run_dir


def run_attacks(
    config_path: Path,
    tier: str | None = None,
    resume_run_id: str | None = None,
    attack_pack_dir: Path | None = None,
    echo: Callable[[str], None] = print,
    headed: bool = False,
) -> Path:
    """Synchronous wrapper around `run_attacks_async`."""
    return asyncio.run(run_attacks_async(config_path, tier, resume_run_id, attack_pack_dir, echo, headed))
