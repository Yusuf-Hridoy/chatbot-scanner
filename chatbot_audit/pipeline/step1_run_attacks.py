"""Step 1: send every selected attack conversation to the chatbot and record the exchanges."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from chatbot_audit.adapters.base_adapter import AdapterError, BaseAdapter, create_adapter
from chatbot_audit.attacks.attack_loader import DEFAULT_PACK_DIR, attack_pack_hash, filter_by_tier, load_attacks
from chatbot_audit.config_loader import ConfigError, load_client_config
from chatbot_audit.data_models import Attack, ClientConfig, Exchange, RunInfo, TurnRecord
from chatbot_audit.pipeline.run_folder import (
    EXCHANGES_FILE,
    RUN_INFO_FILE,
    append_jsonl,
    create_run,
    locate_run,
    read_json,
    read_jsonl,
    repair_jsonl,
    write_json,
)
from chatbot_audit.rate_limiter import RateLimiter

NOT_SENT = "Not sent because an earlier message in this conversation failed."


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _send_with_retry(
    adapter: BaseAdapter, limiter: RateLimiter, config: ClientConfig, conversation_id: str, message: str
):
    """Send one message; on AdapterError pause and retry exactly once."""
    await limiter.wait()
    try:
        return await adapter.send(conversation_id, message)
    except AdapterError:
        await asyncio.sleep(config.limits.retry_pause_seconds)
        await limiter.wait()
        return await adapter.send(conversation_id, message)


async def run_conversation(
    attack: Attack, adapter: BaseAdapter, limiter: RateLimiter, config: ClientConfig, run_id: str
) -> Exchange:
    """Play one attack's turns in a single conversation and return the exchange."""
    started_at = _now()
    turns: list[TurnRecord] = []
    error: str | None = None
    try:
        conversation_id = await adapter.start_conversation()
        for index, message in enumerate(attack.turns):
            try:
                reply = await _send_with_retry(adapter, limiter, config, conversation_id, message)
            except AdapterError as exc:
                error = str(exc)
                turns.append(TurnRecord(index=index, sent=message, reply=None, latency_ms=None, error=error))
                break
            turns.append(TurnRecord(index=index, sent=message, reply=reply.text, latency_ms=reply.latency_ms))
    except AdapterError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - a bug in one conversation must not stop the run
        error = f"Unexpected error: {exc.__class__.__name__}: {exc}"
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
    )


async def run_attacks_async(
    config_path: Path,
    tier: str | None = None,
    resume_run_id: str | None = None,
    attack_pack_dir: Path | None = None,
    echo: Callable[[str], None] = print,
) -> Path:
    """Run (or resume) all attacks for a config and return the run folder."""
    config_path = Path(config_path).resolve()
    config = load_client_config(config_path)

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
    done_ids = {exchange.attack_id for exchange in read_jsonl(exchanges_path, Exchange)}
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
        config_path=str(config_path),
        attack_pack_dir=str(pack_dir),
    )
    write_json(run_dir / RUN_INFO_FILE, info)

    echo(f"Run folder: {run_dir}")
    if done_ids:
        echo(f"Resuming: {len(attacks) - len(pending)} of {len(attacks)} attacks already done.")

    adapter = create_adapter(config)
    limiter = RateLimiter(config.limits.max_messages_per_minute)
    semaphore = asyncio.Semaphore(config.limits.max_parallel_conversations)
    counter = {"done": len(attacks) - len(pending)}

    async def worker(attack: Attack) -> None:
        async with semaphore:
            exchange = await run_conversation(attack, adapter, limiter, config, run_id)
        append_jsonl(exchanges_path, exchange)
        counter["done"] += 1
        sent = sum(1 for turn in exchange.turns if turn.reply is not None)
        latency = sum(turn.latency_ms or 0 for turn in exchange.turns)
        echo(
            f"[{counter['done']}/{len(attacks)}] {attack.id} ... {exchange.status} "
            f"({sent} turn{'s' if sent != 1 else ''}, {latency} ms)"
        )

    try:
        await asyncio.gather(*(worker(attack) for attack in pending))
    finally:
        await adapter.close()

    info.finished_at = _now()
    write_json(run_dir / RUN_INFO_FILE, info)
    return run_dir


def run_attacks(
    config_path: Path,
    tier: str | None = None,
    resume_run_id: str | None = None,
    attack_pack_dir: Path | None = None,
    echo: Callable[[str], None] = print,
) -> Path:
    """Synchronous wrapper around `run_attacks_async`."""
    return asyncio.run(run_attacks_async(config_path, tier, resume_run_id, attack_pack_dir, echo))
