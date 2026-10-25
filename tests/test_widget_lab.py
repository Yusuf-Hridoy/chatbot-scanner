"""The widget lab: hard patterns from real chat widgets, through the browser adapter (needs Chromium).

The browser work is slow one by one, so the module fixture `lab_runs` does all of it at once
(conversations, real `audit check` runs in subprocesses, scripted `audit connect`), a few at a time.
Each test then checks its own result, so a failure still names the lab page and the fix it belongs to.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import Locator, Page, async_playwright

from chatbot_audit.adapters.base_adapter import BotReply, ConversationEvidence
from chatbot_audit.adapters.browser_adapter import BrowserAdapter
from chatbot_audit.connect.check_debug import DEBUG_FILES, DEBUG_FOLDER, REVIEWER_NOTE
from chatbot_audit.connect.selector_recorder import SelectorRecorder, target_yaml
from chatbot_audit.checks.rule_checks import run_check
from chatbot_audit.data_models import CheckSpec, ClientConfig, TurnRecord, Verdict
from chatbot_audit.demo_bot.vulnerable_demo_bot import LAB_PAGES, WIDGET_LAB_DIR, DemoBotServer, respond
from conftest import LAB_EXAMPLE_DIR, _start, bot_port, lab_config_data, make_attack, make_exchange, write_config

pytestmark = pytest.mark.browser

MESSAGES = (
    "How many days do I have to return a product?",
    "What's the minimum order for free shipping?",
    "Am I talking to a human?",
)
HELLO_REPLY = "Hello! How can I help you today?"
QUICK_REPLIES = ["Track my order", "Returns", "Talk to a person"]
NEWSLETTER_MESSAGE = "Can I join your newsletter?"
LAB_PARALLEL = 8  # browser jobs at the same time
AUDIT_CLI = "from chatbot_audit.audit_cli import app; app()"
LAB_SECRETS = ("LAB-COOKIE-7Q2K", "LAB-STORAGE-9X4M")  # set as a cookie and in localStorage by every lab page
SESSION_ID_RE = re.compile(r"lab-\d{10,}")  # the lab's sessionStorage value


# ---------------------------------------------------------------- helpers


@dataclass
class Outcome:
    value: Any = None
    error: BaseException | None = None

    def get(self) -> Any:
        if self.error is not None:
            raise self.error
        return self.value


@dataclass
class CheckResult:
    exit_code: int
    output: str


def lab_adapter(
    server: DemoBotServer, name: str, timeout: float | None = None, query: str = "", **target_fields: Any
) -> BrowserAdapter:
    data = lab_config_data(server, name)
    data["target"]["url"] += query
    data["target"]["selectors"].update(target_fields.pop("selectors", {}))
    data["target"].update(target_fields)
    if timeout is not None:
        data["limits"]["timeout_seconds"] = timeout
    config = ClientConfig.model_validate(data)
    return BrowserAdapter(config.target, config.limits)


async def converse(
    adapter: BrowserAdapter, messages: tuple[str, ...], evidence_dir: Path | None = None, name: str = "lab"
) -> tuple[list[BotReply], ConversationEvidence | None]:
    try:
        conversation = await adapter.start_conversation()
        replies = [await adapter.send(conversation, message) for message in messages]
        evidence = await adapter.end_conversation(conversation, evidence_dir, name) if evidence_dir else None
    finally:
        await adapter.close()
    return replies, evidence


async def audit_check(config_path: Path, *args: str) -> CheckResult:
    """The real `audit check` command in a subprocess (so several can run at once)."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", AUDIT_CLI, "check", "--config", str(config_path), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return CheckResult(process.returncode, output.decode("utf-8", errors="replace"))


def disallowed_codes_verdict(server: DemoBotServer, name: str, sent: str, reply: BotReply) -> Verdict:
    """The disallowed_codes rule check on one browser reply, with the lab page's client config."""
    config = ClientConfig.model_validate(lab_config_data(server, name))
    check = CheckSpec(type="disallowed_codes", severity="critical")
    exchange = make_exchange(reply.text)
    exchange.turns[0] = TurnRecord(index=0, sent=sent, reply=reply.text, latency_ms=reply.latency_ms, buttons=reply.buttons)
    return run_check(check, exchange, config, make_attack(check))


# ---------------------------------------------------------------- scripted `audit connect`

LocatorFor = Callable[[Page], Locator]


class LabScriptedUI:
    """Plays the person at the terminal: performs real clicks on the lab page when prompted."""

    def __init__(
        self, launcher: LocatorFor, message_box: LocatorFor, bot_reply: LocatorFor, send_button: LocatorFor | None = None
    ) -> None:
        self.launcher, self.message_box, self.bot_reply = launcher, message_box, bot_reply
        self.send_button = send_button
        self.page: Page | None = None
        self.lines: list[str] = []
        self.tasks: list[asyncio.Task] = []

    async def _click_when_picking(self, locator: Locator) -> None:
        """Click only once the recorder has switched pick mode on, like a person reading the prompt."""
        await self._wait_for_pick_mode()
        await locator.click()

    async def _wait_for_pick_mode(self) -> None:
        for _ in range(400):
            modes = []
            for frame in self.page.frames:
                try:
                    modes.append(await frame.evaluate("() => window.__auditPickMode"))
                except Exception:  # noqa: BLE001 - frame still loading
                    pass
            if any(modes):
                return
            await asyncio.sleep(0.05)

    def say(self, text: str) -> None:
        self.lines.append(text)
        if text.startswith("Click inside the message box"):
            self.tasks.append(asyncio.ensure_future(self._click_when_picking(self.message_box(self.page))))
        elif text.startswith("Click on the bot's reply"):
            self.tasks.append(asyncio.ensure_future(self._click_when_picking(self.bot_reply(self.page).first)))

    async def wait_for_enter(self, prompt: str) -> None:
        self.lines.append(prompt.strip())
        if prompt.startswith("Close any cookie banners"):
            await self.page.get_by_role("button", name="Accept").click()  # pick mode is off: a normal click
        elif prompt.startswith("Click the chat bubble"):
            await self._click_when_picking(self.launcher(self.page))
            await asyncio.sleep(1.0)  # the recorder sees the click first, then this "Enter"
        elif prompt.startswith("If this chat has a Send button") and self.send_button is not None:
            await self._click_when_picking(self.send_button(self.page))
            await asyncio.sleep(1.0)
        # Otherwise (no Send button, typing indicator): press Enter.

    async def confirm(self, question: str) -> bool:
        self.lines.append(f"{question} y")
        return True


def _in_frame(selector: str) -> LocatorFor:
    return lambda page: page.frame_locator("iframe.lab-frame").locator(selector)


def _in_page(selector: str) -> LocatorFor:
    return lambda page: page.locator(selector)


RECORDED = {
    "frame": None,
    "open_button": '[aria-label="Open chat"]',
    "input": '[aria-label="Message"]',
    "send_button": None,
    "bot_message": "div.msg.msg-bot",
    "typing_indicator": None,
}
CONNECT_CASES: dict[str, tuple[Callable[[], LabScriptedUI], dict[str, Any]]] = {
    "shadow-dom": (
        lambda: LabScriptedUI(_in_page(".lab-launcher"), _in_page(".lab-input"), _in_page(".msg-bot")),
        RECORDED,
    ),
    "cross-origin-iframe": (
        lambda: LabScriptedUI(_in_page(".lab-launcher"), _in_frame(".lab-input"), _in_frame(".msg-bot")),
        {**RECORDED, "frame": "iframe.lab-frame"},
    ),
    # Enter does nothing here: the recorder must record the Send button and use it.
    "send-button-only": (
        lambda: LabScriptedUI(
            _in_page(".lab-launcher"), _in_page(".lab-input"), _in_page(".msg-bot"), send_button=_in_page(".lab-send")
        ),
        {**RECORDED, "send_button": "button.lab-send"},
    ),
}


class WrongClicksUI(LabScriptedUI):
    """A person who makes mistakes on the shadow-dom page: presses Enter at the launcher step while the
    chat is still closed, then clicks the page background at the launcher and message box steps."""

    def __init__(self) -> None:
        super().__init__(_in_page(".lab-launcher"), _in_page(".lab-input"), _in_page(".msg-bot"))
        self.launcher_prompts = 0

    async def _click_background(self) -> None:
        await self._wait_for_pick_mode()
        await self.page.mouse.click(5, 790)  # empty page: the <html> or <body> element
        await asyncio.sleep(0.5)

    async def _background_then(self, locator: Locator) -> None:
        await self._click_background()
        await self._click_when_picking(locator)

    def say(self, text: str) -> None:
        if text.startswith("Click inside the message box"):
            self.lines.append(text)
            self.tasks.append(asyncio.ensure_future(self._background_then(self.message_box(self.page))))
        else:
            super().say(text)

    async def wait_for_enter(self, prompt: str) -> None:
        if prompt.startswith("Click the chat bubble"):
            self.launcher_prompts += 1
            self.lines.append(prompt.strip())
            if self.launcher_prompts == 1:
                return  # Enter, but the chat is still closed
            await self._background_then(self.launcher(self.page))
            await asyncio.sleep(1.0)
            return
        await super().wait_for_enter(prompt)


async def record_and_check(
    server: DemoBotServer, name: str, folder: Path, ui: LabScriptedUI | None = None
) -> dict[str, Any]:
    """Drive `audit connect` with scripted clicks, then run `audit check` with only the recorded target."""
    ui = ui or CONNECT_CASES[name][0]()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 800})
            recorder = await SelectorRecorder.attach(context, ui, pick_timeout=30)
            ui.page = recorder.page
            target = await recorder.record(f"http://127.0.0.1:{bot_port(server)}/lab/{name}")
        finally:
            await browser.close()
    data = lab_config_data(server, name)
    data["target"] = target
    check = await audit_check(write_config(folder, data))
    return {"target": target, "transcript": ui.lines, "check": check}


# ---------------------------------------------------------------- all browser work, a few jobs at a time


@pytest.fixture(scope="module")
def lab_server() -> Iterator[DemoBotServer]:
    yield from _start(safe_mode=False)


@pytest.fixture(scope="module")
def lab_runs(lab_server: DemoBotServer, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Outcome]:
    server = lab_server
    base = tmp_path_factory.mktemp("lab")

    def broken_input_config() -> Path:
        data = lab_config_data(server, "shadow-dom")
        data["target"]["selectors"]["input"] = "#no-such-input"
        data["target"]["page_load_timeout_seconds"] = 3
        return write_config(base / "debug-failed", data)

    jobs: dict[str, Callable[[], Awaitable[Any]]] = {}
    # Slow jobs first, so they overlap with everything else.
    for name in sorted(LAB_PAGES, key=lambda page: page != "slow-reply"):
        jobs[f"conversation:{name}"] = lambda name=name: converse(
            lab_adapter(server, name), MESSAGES, base / "evidence" / name, name
        )
        jobs[f"check:{name}"] = lambda name=name: audit_check(
            write_config(base / "check" / name, lab_config_data(server, name))
        )
    for name in CONNECT_CASES:
        jobs[f"connect:{name}"] = lambda name=name: record_and_check(server, name, base / "connect" / name)
    jobs["connect:wrong-clicks"] = lambda: record_and_check(
        server, "shadow-dom", base / "connect" / "wrong-clicks", WrongClicksUI()
    )
    jobs["fix:welcome"] = lambda: converse(lab_adapter(server, "welcome-message"), ("Hi",))
    # The page asks for an email in a bot-like bubble, then the bot's own reply also mentions email.
    jobs["fix:newsletter"] = lambda: converse(lab_adapter(server, "system-bubble"), (NEWSLETTER_MESSAGE,))
    # ?promo=1 adds a "Get code SORRY50" quick reply button.
    jobs["fix:promo-button"] = lambda: converse(lab_adapter(server, "quick-replies", query="?promo=1"), ("Hello",))
    # The reply timeout (3 s) is shorter than the 5 s the launcher takes to appear.
    jobs["fix:late-load"] = lambda: converse(
        lab_adapter(server, "late-load", timeout=3, page_load_timeout_seconds=15), ("Hi",)
    )
    # A selector list that matches a bubble and an element inside it (the Crisp preset does this).
    jobs["fix:nested"] = lambda: converse(
        lab_adapter(server, "quick-replies", selectors={"bot_message": ".msg-bot, .msg-bot .msg-text"}), ("Hi",)
    )
    jobs["debug:failed"] = lambda: audit_check(broken_input_config())
    jobs["debug:flag"] = lambda: audit_check(
        write_config(base / "debug-flag", lab_config_data(server, "cross-origin-iframe")), "--debug"
    )

    async def run_all() -> dict[str, Outcome]:
        semaphore = asyncio.Semaphore(LAB_PARALLEL)

        async def run(job: Callable[[], Awaitable[Any]]) -> Outcome:
            async with semaphore:
                try:
                    return Outcome(value=await job())
                except Exception as exc:  # noqa: BLE001 - reported by the test that owns this job
                    return Outcome(error=exc)

        outcomes = await asyncio.gather(*(run(job) for job in jobs.values()))
        return dict(zip(jobs, outcomes))

    return asyncio.run(run_all())


# ---------------------------------------------------------------- lab pages are served correctly


def test_every_lab_page_has_a_page_and_a_config() -> None:
    for name in LAB_PAGES:
        assert (WIDGET_LAB_DIR / f"{name}.html").is_file(), name
        assert (LAB_EXAMPLE_DIR / f"{name}.yaml").is_file(), name
    assert len(LAB_PAGES) == 12


def test_lab_index_links_every_page(lab_server) -> None:
    html = httpx.get(f"http://127.0.0.1:{bot_port(lab_server)}/lab").text
    for name in LAB_PAGES:
        assert f'href="/lab/{name}"' in html


def test_lab_paths_cannot_leave_the_lab_folder(lab_server) -> None:
    base = f"http://127.0.0.1:{bot_port(lab_server)}/lab/"
    for path in ("..%2Fvulnerable_demo_bot.py", "%2E%2E/demo_site/shop_page.html", "lab_common.py", "Shadow-Dom"):
        assert httpx.get(base + path).status_code == 404, path


def test_cross_origin_panel_comes_from_another_port(lab_server) -> None:
    html = httpx.get(f"http://127.0.0.1:{bot_port(lab_server)}/lab/cross-origin-iframe").text
    assert f"http://127.0.0.1:{lab_server.frame_port}/lab/cross-origin-panel" in html
    assert lab_server.frame_port != bot_port(lab_server)


# ---------------------------------------------------------------- every lab page: replies, evidence, audit check


@pytest.mark.parametrize("name", list(LAB_PAGES))
def test_lab_page_conversation(lab_runs, name: str) -> None:
    replies, evidence = lab_runs[f"conversation:{name}"].get()
    assert [reply.text for reply in replies] == [respond(message, False, False) for message in MESSAGES]
    expected_buttons = QUICK_REPLIES if name == "quick-replies" else []
    assert all(reply.buttons == expected_buttons for reply in replies)
    assert evidence.screenshot_path.stat().st_size > 0
    assert evidence.screenshot_path.with_suffix(".page.png").stat().st_size > 0
    assert evidence.video_path is not None and evidence.video_path.stat().st_size > 0


@pytest.mark.parametrize("name", list(LAB_PAGES))
def test_lab_page_audit_check(lab_runs, name: str) -> None:
    result = lab_runs[f"check:{name}"].get()
    assert result.exit_code == 0, result.output
    assert f"Reply:   {HELLO_REPLY}\n" in result.output
    assert "Screenshot: " in result.output and "check_screenshot.png" in result.output
    if name == "quick-replies":
        assert f"Buttons: {', '.join(QUICK_REPLIES)}\n" in result.output
    if name == "slow-reply":
        assert (
            "Still waiting for a reply (10 s of 30 s). If you are replying by hand, reply now in your chat inbox."
            in result.output
        )


# ---------------------------------------------------------------- focused tests for the fixes


def test_welcome_message_is_never_part_of_a_reply(lab_runs) -> None:
    replies, _ = lab_runs["fix:welcome"].get()
    assert [reply.text for reply in replies] == [HELLO_REPLY]


def test_late_widget_waits_for_page_load_timeout(lab_runs) -> None:
    replies, _ = lab_runs["fix:late-load"].get()
    assert [reply.text for reply in replies] == [HELLO_REPLY]


def test_nested_bot_message_matches_are_read_once(lab_runs) -> None:
    replies, _ = lab_runs["fix:nested"].get()
    assert [reply.text for reply in replies] == [HELLO_REPLY]


def test_short_email_bubble_is_skipped_but_a_reply_with_a_code_is_not(lab_runs, lab_server) -> None:
    [reply], _ = lab_runs["fix:newsletter"].get()
    # The page's "Leave your email so we can reply if you leave the chat." bubble is skipped (it fully matches).
    # The bot's reply also says "Leave your email", but as part of a longer message, so it is kept.
    assert reply.text == respond(NEWSLETTER_MESSAGE, False, False)
    assert reply.text == "Sure! Leave your email and I'll send you a 50% code SORRY50."
    verdict = disallowed_codes_verdict(lab_server, "system-bubble", NEWSLETTER_MESSAGE, reply)
    assert (verdict.verdict, verdict.matched_text) == ("fail", "SORRY50")


def test_code_on_a_quick_reply_button_is_a_finding(lab_runs, lab_server) -> None:
    [reply], _ = lab_runs["fix:promo-button"].get()
    assert reply.text == HELLO_REPLY  # the reply text has no code at all
    assert reply.buttons == [*QUICK_REPLIES, "Get code SORRY50"]
    verdict = disallowed_codes_verdict(lab_server, "quick-replies", "Hello", reply)
    assert (verdict.verdict, verdict.matched_text) == ("fail", "SORRY50")


@pytest.mark.parametrize("name", list(CONNECT_CASES))
def test_audit_connect_records_working_selectors(lab_runs, name: str) -> None:
    run = lab_runs[f"connect:{name}"].get()
    print(f"\n--- audit connect on {name} ---\n" + "\n".join(run["transcript"]) + "\n" + target_yaml(run["target"]))
    assert (
        "If this chat has a Send button, click it now. If pressing Enter sends messages, press Enter here."
        in run["transcript"]
    )
    assert run["target"]["selectors"] == CONNECT_CASES[name][1]
    check = run["check"]
    assert check.exit_code == 0, check.output  # `audit check` with only the recorded target block
    assert f"Reply:   {HELLO_REPLY}\n" in check.output


def test_audit_connect_recovers_from_wrong_clicks(lab_runs) -> None:
    run = lab_runs["connect:wrong-clicks"].get()
    transcript = run["transcript"]
    print("\n--- audit connect with wrong clicks ---\n" + "\n".join(transcript))
    launcher_prompt = "Click the chat bubble that opens the chat. If the chat box is already open, press Enter here."
    assert transcript.count(launcher_prompt) == 2  # asked again after Enter with a closed chat
    assert "I can't see an open chat box. Please click the chat bubble." in transcript
    assert "That click was on the page background. Please click the chat bubble." in transcript
    assert "That is not a message box. Please click inside the box where customers type." in transcript
    assert run["target"]["selectors"] == RECORDED
    assert "html" not in target_yaml(run["target"]) and "body" not in target_yaml(run["target"])
    check = run["check"]
    assert check.exit_code == 0, check.output
    assert f"Reply:   {HELLO_REPLY}\n" in check.output


# ---------------------------------------------------------------- audit check debug folder


def _debug_text(folder: Path) -> str:
    return "".join(path.read_text(encoding="utf-8") for path in folder.iterdir() if path.suffix != ".png")


def _debug_folder(result: CheckResult) -> Path:
    match = re.search(r"Debug files saved to (.+)", result.output)
    assert match, result.output
    return Path(match.group(1).strip())


def test_failed_check_writes_debug_folder(lab_runs) -> None:
    result = lab_runs["debug:failed"].get()
    assert result.exit_code == 2, result.output
    folder = _debug_folder(result)
    assert folder.name == DEBUG_FOLDER
    assert REVIEWER_NOTE in result.output
    assert sorted(path.name for path in folder.iterdir()) == sorted(DEBUG_FILES)
    assert (folder / "page.png").stat().st_size > 0
    summary = (folder / "summary.txt").read_text(encoding="utf-8")
    print("\n--- summary.txt ---\n" + summary)
    assert 'failed at the "input" step (finding the message box)' in summary
    assert "Selector used: #no-such-input" in summary
    assert '1. [aria-label="Message"]' in summary  # the real message box (inside the shadow root) comes first
    everything = _debug_text(folder)
    for secret in LAB_SECRETS:
        assert secret not in everything
    assert not SESSION_ID_RE.search(everything)


def test_debug_flag_saves_folder_when_check_works(lab_runs, lab_server) -> None:
    result = lab_runs["debug:flag"].get()
    assert result.exit_code == 0, result.output
    folder = _debug_folder(result)
    assert "the check worked" in (folder / "summary.txt").read_text(encoding="utf-8")
    frames = json.loads((folder / "frames.json").read_text(encoding="utf-8"))
    panel_frames = [frame for frame in frames if frame["iframe_selector"] == "iframe.lab-frame"]
    assert len(panel_frames) == 1 and f":{lab_server.frame_port}/" in panel_frames[0]["url"]
    candidates = json.loads((folder / "candidates.json").read_text(encoding="utf-8"))
    inputs = [item for item in candidates if item["kind"] == "input" and item["frame_url"] == panel_frames[0]["url"]]
    assert inputs and all(item["text"] == "" for item in inputs)  # never the typed value
    everything = _debug_text(folder)
    for secret in LAB_SECRETS:
        assert secret not in everything
