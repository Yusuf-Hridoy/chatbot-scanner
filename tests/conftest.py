"""Shared fixtures: demo bot servers on free ports and temporary client configs."""

from __future__ import annotations

import functools
import os
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
BROWSER_EXAMPLE = EXAMPLE_DIR / "client_config_browser.yaml"
EXAMPLES = {"http": HTTP_EXAMPLE, "openai": OPENAI_EXAMPLE, "browser": BROWSER_EXAMPLE}
LAB_EXAMPLE_DIR = REPO_ROOT / "examples" / "widget_lab"
CHROMIUM_SKIP_REASON = "Chromium not installed. Run: playwright install chromium"


@functools.cache
def chromium_available() -> bool:
    """Try once per session to launch Chromium. AUDIT_TEST_NO_CHROMIUM=1 simulates a missing browser."""
    if os.environ.get("AUDIT_TEST_NO_CHROMIUM") == "1":
        return False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            playwright.chromium.launch().close()
    except Exception:  # noqa: BLE001 - any failure means "not usable here"
        return False
    return True


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip (never fail) browser-marked tests when Chromium cannot start."""
    browser_items = [item for item in items if item.get_closest_marker("browser")]
    if browser_items and not chromium_available():
        for item in browser_items:
            item.add_marker(pytest.mark.skip(reason=CHROMIUM_SKIP_REASON))


def _start(safe_mode: bool) -> Iterator[DemoBotServer]:
    server = make_server("127.0.0.1", 0, safe_mode)
    # A short poll interval makes shutdown (after every test) quick.
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
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
        data = yaml.safe_load(EXAMPLES[kind].read_text(encoding="utf-8"))
        port = bot_port(server)
        if kind == "http":
            data["target"]["url"] = f"http://127.0.0.1:{port}/chat"
        elif kind == "openai":
            data["target"]["base_url"] = f"http://127.0.0.1:{port}/v1"
        else:
            data["target"]["url"] = f"http://127.0.0.1:{port}/"
            data["target"]["reply_settle_ms"] = 600
        data["limits"] = {
            "max_messages_per_minute": 6000,
            "max_parallel_conversations": 3 if kind == "browser" else 4,
            "timeout_seconds": 20 if kind == "browser" else 5,
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


def browser_target(server: DemoBotServer, path: str = "/", **fields: Any) -> dict[str, Any]:
    """A browser target dict for the demo website, with test-friendly timings."""
    target: dict[str, Any] = {
        "type": "browser",
        "url": f"http://127.0.0.1:{bot_port(server)}{path}",
        "preset": "generic",
        "selectors": {"frame": "iframe[data-chat-frame]"},
        "reply_settle_ms": 600,
    }
    target.update(fields)
    return target


def lab_config_data(server: DemoBotServer, name: str) -> dict[str, Any]:
    """The widget lab config examples/widget_lab/<name>.yaml, pointed at a demo bot on a free port."""
    data = yaml.safe_load((LAB_EXAMPLE_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    data["target"]["url"] = data["target"]["url"].replace("127.0.0.1:8765", f"127.0.0.1:{bot_port(server)}")
    return data


def write_config(folder: Path, data: dict[str, Any]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "client_config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path
