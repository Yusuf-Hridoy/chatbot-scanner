"""Shared fixtures: demo bot servers on free ports and temporary client configs."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from chatbot_audit.data_models import Attack, CheckSpec, ClientConfig, Exchange, TurnRecord
from chatbot_audit.demo_bot.vulnerable_demo_bot import DemoBotServer, make_server

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_DIR = REPO_ROOT / "examples" / "demo_client"
HTTP_EXAMPLE = EXAMPLE_DIR / "client_config.yaml"
OPENAI_EXAMPLE = EXAMPLE_DIR / "client_config_openai.yaml"


def _start(safe_mode: bool) -> Iterator[DemoBotServer]:
    server = make_server("127.0.0.1", 0, safe_mode)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def unsafe_bot() -> Iterator[DemoBotServer]:
    """The deliberately vulnerable demo bot on a free port."""
    yield from _start(safe_mode=False)


@pytest.fixture
def safe_bot() -> Iterator[DemoBotServer]:
    """The fixed (safe) demo bot on a free port."""
    yield from _start(safe_mode=True)


def bot_port(server: DemoBotServer) -> int:
    return server.server_address[1]


@pytest.fixture
def make_config(tmp_path: Path) -> Callable[..., Path]:
    """Write a temporary client config (based on the examples) that points at a demo bot."""

    def factory(server: DemoBotServer, kind: str = "http", **overrides: Any) -> Path:
        source = HTTP_EXAMPLE if kind == "http" else OPENAI_EXAMPLE
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
        port = bot_port(server)
        if kind == "http":
            data["target"]["url"] = f"http://127.0.0.1:{port}/chat"
        else:
            data["target"]["base_url"] = f"http://127.0.0.1:{port}/v1"
        data["limits"] = {
            "max_messages_per_minute": 6000,
            "max_parallel_conversations": 4,
            "timeout_seconds": 5,
            "retry_pause_seconds": 0.05,
        }
        data.update(overrides)
        folder = tmp_path / f"client_{kind}"
        folder.mkdir(exist_ok=True)
        path = folder / "client_config.yaml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return path

    return factory


# ---------------------------------------------------------------- helpers for unit tests


def demo_config(**overrides: Any) -> ClientConfig:
    """The HTTP example config as a model (no file, no network)."""
    data = yaml.safe_load(HTTP_EXAMPLE.read_text(encoding="utf-8"))
    data.update(overrides)
    return ClientConfig.model_validate(data)


def make_attack(*checks: CheckSpec, severity: str = "high", turns: list[str] | None = None) -> Attack:
    return Attack(
        id="test-attack",
        title="Test attack",
        category="false_promises",
        owasp="LLM09",
        severity=severity,  # type: ignore[arg-type]
        tiers=["standard"],
        turns=turns or ["hello"],
        checks=list(checks),
        why_it_matters="Because.",
        how_to_fix="Fix it.",
    )


def make_exchange(*replies: str | None, status: str = "completed", error: str | None = None) -> Exchange:
    now = datetime.now(timezone.utc)
    turns = [
        TurnRecord(index=i, sent=f"message {i}", reply=reply, latency_ms=10 if reply is not None else None)
        for i, reply in enumerate(replies)
    ]
    return Exchange(
        run_id="test-run",
        attack_id="test-attack",
        status=status,  # type: ignore[arg-type]
        turns=turns,
        error=error,
        started_at=now,
        finished_at=now,
    )
