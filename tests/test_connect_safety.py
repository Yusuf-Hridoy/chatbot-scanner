"""`audit connect` safety: stray Enter presses are thrown away, and browser errors never show a traceback."""

from __future__ import annotations

import asyncio
import socket
import sys
import types
from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError
from typer.testing import CliRunner

from chatbot_audit.audit_cli import app
from chatbot_audit.config_loader import ConfigError
from chatbot_audit.connect import selector_recorder
from chatbot_audit.connect.selector_recorder import TerminalUI, discard_typed_keys, run_connect


class FakeStdin:
    def __init__(self, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty

    def fileno(self) -> int:
        return 0


# ---------------------------------------------------------------- 1. stray key presses


def test_discard_does_nothing_when_stdin_is_not_a_terminal(monkeypatch) -> None:
    termios = pytest.importorskip("termios")
    calls = []
    monkeypatch.setattr(termios, "tcflush", lambda *args: calls.append(args))
    monkeypatch.setattr(sys, "platform", "linux")
    discard_typed_keys(FakeStdin(tty=False))
    assert calls == []


def test_discard_flushes_typed_keys_on_mac_and_linux(monkeypatch) -> None:
    termios = pytest.importorskip("termios")
    calls = []
    monkeypatch.setattr(termios, "tcflush", lambda *args: calls.append(args))
    monkeypatch.setattr(sys, "platform", "darwin")
    stdin = FakeStdin(tty=True)
    discard_typed_keys(stdin)
    assert calls == [(stdin, termios.TCIFLUSH)]


def test_discard_reads_waiting_keys_on_windows(monkeypatch) -> None:
    waiting = ["\r", "\r", "x"]
    read = []
    fake_msvcrt = types.SimpleNamespace(kbhit=lambda: bool(waiting), getwch=lambda: read.append(waiting.pop(0)))
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(sys, "platform", "win32")
    discard_typed_keys(FakeStdin(tty=True))
    assert read == ["\r", "\r", "x"] and waiting == []


def test_terminal_ui_discards_keys_before_every_prompt(monkeypatch) -> None:
    events = []
    monkeypatch.setattr(selector_recorder, "discard_typed_keys", lambda *args: events.append("discard"))
    monkeypatch.setattr("builtins.input", lambda prompt="": events.append(f"input:{prompt.strip()}") or "")
    ui = TerminalUI()
    asyncio.run(ui.wait_for_enter("Press Enter. "))
    assert asyncio.run(ui.confirm("Is this the bot's reply?")) is True
    assert events == ["discard", "input:Press Enter.", "discard", "input:Is this the bot's reply? [Y/n]"]


# ---------------------------------------------------------------- 4. no tracebacks


def test_connect_command_prints_one_line_for_browser_errors(monkeypatch) -> None:
    async def broken_connect(*args, **kwargs):
        raise PlaywrightError("Locator.click: Timeout 30000ms exceeded.\nCall log:\n  - waiting for locator('#x')")

    monkeypatch.setattr(selector_recorder, "run_connect", broken_connect)
    result = CliRunner().invoke(app, ["connect", "--url", "http://127.0.0.1:9/"])
    assert result.exit_code == 1
    assert not isinstance(result.exception, PlaywrightError), result.exception
    assert "Traceback" not in result.output and "Call log" not in result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert lines == [
        "Error: The browser stopped working (Locator.click: Timeout 30000ms exceeded.). Run 'audit connect' again."
    ]


def test_connect_command_says_when_the_window_was_closed(monkeypatch) -> None:
    async def closed(*args, **kwargs):
        raise PlaywrightError("Target page, context or browser has been closed")

    monkeypatch.setattr(selector_recorder, "run_connect", closed)
    result = CliRunner().invoke(app, ["connect", "--url", "http://127.0.0.1:9/"])
    assert result.exit_code == 1
    assert result.output.strip() == "Error: The browser window was closed. Run 'audit connect' again."


@pytest.mark.browser
def test_run_connect_on_a_dead_address_is_one_plain_line(tmp_path: Path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]  # nothing listens here once the socket is closed
    url = f"http://127.0.0.1:{port}/"
    with pytest.raises(ConfigError) as caught:
        asyncio.run(run_connect(url, tmp_path / "target.yaml", headless=True))
    assert str(caught.value) == f"Could not open {url}. Check the address and that the site is running."
    assert not (tmp_path / "target.yaml").exists()
