"""Interactive point-and-click recorder that writes the `target:` block for a browser config."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml
from playwright.async_api import BrowserContext, Frame, FrameLocator, Page, async_playwright
from playwright.async_api import Error as PlaywrightError

from chatbot_audit.config_loader import ConfigError

SELECTOR_BUILDER_JS = Path(__file__).resolve().parent / "selector_builder.js"
PICK_TIMEOUT_SECONDS = 120.0
FIRST_MESSAGE = "Hello"
SECOND_MESSAGE = "What can you help with?"
CLOSE_POPUPS_PROMPT = "Close any cookie banners or popups on the page, then press Enter. "
LAUNCHER_PROMPT = "Click the chat bubble that opens the chat. If the chat box is already open, press Enter here. "
CHAT_NOT_OPEN = "I can't see an open chat box. Please click the chat bubble."
NOT_A_MESSAGE_BOX = "That is not a message box. Please click inside the box where customers type."
# Said when a click lands on the page itself (<html> or <body>) instead of a chat element.
BACKGROUND_LAUNCHER = "That click was on the page background. Please click the chat bubble."
BACKGROUND_SEND = "That click was on the page background. Please click the Send button, or press Enter here."
BACKGROUND_REPLY = "That click was on the page background. Please click the bot's reply."
BACKGROUND_TYPING = "That click was on the page background. Please click the typing animation, or press Enter."
BROWSER_CLOSED = "The browser window was closed. Run 'audit connect' again."
SEND_BUTTON_PROMPT = "If this chat has a Send button, click it now. If pressing Enter sends messages, press Enter here. "


class RecorderUI(Protocol):
    """How the recorder talks to the person (the terminal in real use, a script in tests)."""

    def say(self, text: str) -> None: ...

    async def wait_for_enter(self, prompt: str) -> None: ...

    async def confirm(self, question: str) -> bool: ...


def discard_typed_keys(stream: Any = None) -> None:
    """Throw away keys typed before a prompt (an Enter pressed during a click step must not answer it).

    Does nothing if stdin is not a terminal.
    """
    stream = sys.stdin if stream is None else stream
    try:
        if stream is None or not stream.isatty():
            return
    except (AttributeError, ValueError):
        return
    if sys.platform == "win32":
        import msvcrt

        while msvcrt.kbhit():
            msvcrt.getwch()
    else:
        import termios

        try:
            termios.tcflush(stream, termios.TCIFLUSH)
        except (termios.error, OSError, ValueError):
            pass


class TerminalUI:
    """Prompts in the terminal. input() runs in a thread so browser clicks are captured meanwhile."""

    def say(self, text: str) -> None:
        print(text, flush=True)

    async def wait_for_enter(self, prompt: str) -> None:
        discard_typed_keys()
        await asyncio.to_thread(input, prompt)

    async def confirm(self, question: str) -> bool:
        discard_typed_keys()
        answer = await asyncio.to_thread(input, f"{question} [Y/n] ")
        return answer.strip().lower() not in ("n", "no")


@dataclass
class Pick:
    selector: str | None   # None when the click was on the page background (<html> or <body>)
    text: str
    frame: Frame
    textbox: bool = False  # the click was inside a box a customer can type into


class SelectorRecorder:
    """Guides the user through closing popups, then clicking the launcher, input, Send button, bot reply
    and typing indicator."""

    def __init__(
        self,
        context: BrowserContext,
        page: Page,
        ui: RecorderUI,
        frame_auto: bool = True,
        pick_timeout: float = PICK_TIMEOUT_SECONDS,
    ) -> None:
        self.context = context
        self.page = page
        self.ui = ui
        self.frame_auto = frame_auto
        self.pick_timeout = pick_timeout
        self.picks: asyncio.Queue[Pick] = asyncio.Queue()

    @classmethod
    async def attach(cls, context: BrowserContext, ui: RecorderUI, **kwargs: Any) -> SelectorRecorder:
        """Install the click-capture script and binding on a context, then open a page."""
        recorder_holder: dict[str, SelectorRecorder] = {}

        def on_pick(source: dict[str, Any], payload: dict[str, Any]) -> None:
            recorder = recorder_holder.get("recorder")
            if recorder is not None:
                recorder.picks.put_nowait(
                    Pick(
                        payload.get("selector") or None,
                        payload.get("text") or "",
                        source["frame"],
                        bool(payload.get("textbox")),
                    )
                )

        await context.expose_binding("__auditPick", on_pick)
        await context.add_init_script(path=str(SELECTOR_BUILDER_JS))
        page = await context.new_page()
        recorder = cls(context, page, ui, **kwargs)
        recorder_holder["recorder"] = recorder
        return recorder

    # ------------------------------------------------------------ pick helpers

    async def _set_pick_mode(self, mode: str | None) -> None:
        for frame in self.page.frames:
            try:
                await frame.evaluate("mode => { window.__auditPickMode = mode; }", mode)
            except PlaywrightError:
                pass  # frame navigated away or detached

    def _drain_picks(self) -> None:
        while not self.picks.empty():
            self.picks.get_nowait()

    async def _next_pick(self, retry: str, need_textbox: bool = False) -> Pick:
        """The next usable click. Clicks on the page background (or, for the message box, on anything
        that is not a text box) are refused with `retry`, and the person is asked again."""
        while True:
            pick = await self.picks.get()
            if pick.selector and (pick.textbox or not need_textbox):
                return pick
            self.ui.say(retry)

    async def _pick(self, mode: str, retry: str, need_textbox: bool = False) -> Pick:
        """Wait for one click in pick mode; clicks are captured instead of acting on the page."""
        self._drain_picks()
        await self._set_pick_mode(mode)
        try:
            return await asyncio.wait_for(self._next_pick(retry, need_textbox), timeout=self.pick_timeout)
        except asyncio.TimeoutError as exc:
            raise ConfigError(f"No click received within {self.pick_timeout:g} seconds. Run 'audit connect' again.") from exc
        finally:
            await self._set_pick_mode(None)

    async def _pick_or_enter(self, mode: str, prompt: str, retry: str) -> Pick | None:
        """Wait for a click, or for Enter in the terminal (returns None)."""
        self._drain_picks()
        await self._set_pick_mode(mode)
        pick_task = asyncio.ensure_future(self._next_pick(retry))
        enter_task = asyncio.ensure_future(self.ui.wait_for_enter(prompt))
        try:
            done, _ = await asyncio.wait(
                {pick_task, enter_task}, timeout=self.pick_timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            await self._set_pick_mode(None)
        if pick_task in done:
            if not enter_task.done():
                self.ui.say("Got it. Press Enter here to continue.")
                await enter_task
            return pick_task.result()
        pick_task.cancel()
        if enter_task in done:
            return None
        enter_task.cancel()
        raise ConfigError(f"No click or Enter received within {self.pick_timeout:g} seconds.")

    async def _frame_selector(self, frame: Frame) -> str | None:
        """Selector of the <iframe> element (in its parent page) that contains `frame`."""
        if frame == self.page.main_frame or not self.frame_auto:
            return None
        element = await frame.frame_element()
        return await element.evaluate("el => window.__auditBuildSelector(el, 'unique')")

    async def _chat_box_visible(self) -> bool:
        """True if a message box is visible on the page, in an open shadow root or in a visible iframe."""
        for frame in self.page.frames:
            try:
                if frame != self.page.main_frame:
                    element = await frame.frame_element()
                    if not await element.is_visible():
                        continue
                if await frame.evaluate("() => !!(window.__auditHasVisibleTextbox && window.__auditHasVisibleTextbox())"):
                    return True
            except PlaywrightError:
                continue  # frame still loading or gone
        return False

    def _scope(self, frame_selector: str | None) -> Page | FrameLocator:
        return self.page.frame_locator(frame_selector) if frame_selector else self.page

    # ------------------------------------------------------------ the guided flow

    async def record(self, url: str) -> dict[str, Any]:
        """Run the guided steps and return the `target:` block as a dict."""
        self.ui.say("The page is loading…")
        try:
            await self.page.goto(url)
        except PlaywrightError as exc:
            raise ConfigError(f"Could not open {url}. Check the address and that the site is running.") from exc

        # Pick mode is off here, so clicks act on the page normally.
        await self._set_pick_mode(None)
        await self.ui.wait_for_enter(CLOSE_POPUPS_PROMPT)

        while True:
            launcher = await self._pick_or_enter("unique", LAUNCHER_PROMPT, BACKGROUND_LAUNCHER)
            if launcher is not None or await self._chat_box_visible():
                break
            self.ui.say(CHAT_NOT_OPEN)  # Enter was pressed, but the chat is still closed
        open_button = None
        if launcher is not None:
            open_button = launcher.selector
            self.ui.say(f"  Chat launcher: {open_button}")
            frame_of_launcher = launcher.frame
            await frame_of_launcher.locator(open_button).first.click()  # actually open the chat now

        self.ui.say("Click inside the message box where customers type.")
        picked_input = await self._pick("textbox", NOT_A_MESSAGE_BOX, need_textbox=True)
        frame = await self._frame_selector(picked_input.frame)
        self.ui.say(f"  Message box: {picked_input.selector}" + (f" (inside iframe {frame})" if frame else ""))
        scope = self._scope(frame)
        input_locator = scope.locator(picked_input.selector).first

        # Pick mode catches this click, so it does not send anything yet.
        picked_send = await self._pick_or_enter("unique", SEND_BUTTON_PROMPT, BACKGROUND_SEND)
        send_button = picked_send.selector if picked_send else None
        if send_button:
            self.ui.say(f"  Send button: {send_button}")

        await self._send_message(scope, input_locator, send_button, FIRST_MESSAGE)
        self.ui.say(f'Sent "{FIRST_MESSAGE}". Wait for the bot to answer.')

        bot_message = await self._pick_bot_message(scope)

        self.ui.say(
            "If a typing animation appears while the bot replies, click it now during the next reply, "
            "or press Enter to skip."
        )
        await self._send_message(scope, input_locator, send_button, SECOND_MESSAGE)
        typing = await self._pick_or_enter("all_similar", "Press Enter to skip the typing indicator. ", BACKGROUND_TYPING)
        typing_indicator = typing.selector if typing else None
        if typing_indicator:
            self.ui.say(f"  Typing indicator: {typing_indicator}")

        return {
            "type": "browser",
            "url": url,
            "preset": "generic",
            "selectors": {
                "frame": frame,
                "open_button": open_button,
                "input": picked_input.selector,
                "send_button": send_button,
                "bot_message": bot_message,
                "typing_indicator": typing_indicator,
            },
        }

    @staticmethod
    async def _send_message(
        scope: Page | FrameLocator, input_locator: Any, send_button: str | None, message: str
    ) -> None:
        """Type a message and send it with the Send button, or with Enter if there is none."""
        await input_locator.fill(message)
        if send_button:
            await scope.locator(send_button).first.click()
        else:
            await input_locator.press("Enter")

    async def _pick_bot_message(self, scope: Page | FrameLocator) -> str:
        while True:
            self.ui.say("Click on the bot's reply.")
            pick = await self._pick("all_similar", BACKGROUND_REPLY)
            matches = scope.locator(pick.selector)
            texts = [text.strip() for text in await matches.all_inner_texts()]
            if not texts or not texts[-1]:
                self.ui.say(f"  '{pick.selector}' did not match any visible text. Please click the reply again.")
                continue
            if FIRST_MESSAGE in texts:
                self.ui.say(f"  '{pick.selector}' also matches your own message. Please click the bot's reply.")
                continue
            self.ui.say(f"  Bot messages: {pick.selector} ({len(texts)} match)")
            self.ui.say(f"  Last reply: {texts[-1]}")
            if await self.ui.confirm("Is this the bot's reply?"):
                return pick.selector


def browser_error_message(exc: Exception) -> str:
    """One short line for a Playwright error (no call log, no traceback)."""
    text = str(exc).strip()
    if "has been closed" in text or "Target closed" in text:
        return BROWSER_CLOSED
    first = text.splitlines()[0][:150] if text else exc.__class__.__name__
    return f"The browser stopped working ({first}). Run 'audit connect' again."


def target_yaml(target: dict[str, Any]) -> str:
    """The `target:` block as YAML, ready to paste into a client config."""
    return yaml.safe_dump({"target": target}, sort_keys=False, allow_unicode=True)


async def run_connect(url: str, out_path: Path, frame_auto: bool = True, headless: bool = False) -> Path:
    """Open Chromium, run the guided recorder, print and save the target block."""
    ui = TerminalUI()
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=headless)
        except PlaywrightError as exc:
            raise ConfigError("Chromium could not be started. Install it with: playwright install chromium") from exc
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 800})
            recorder = await SelectorRecorder.attach(context, ui, frame_auto=frame_auto)
            target = await recorder.record(url)
        except PlaywrightError as exc:
            raise ConfigError(browser_error_message(exc)) from exc
        finally:
            try:
                await browser.close()
            except PlaywrightError:
                pass  # already closed by the person
    text = target_yaml(target)
    out_path = Path(out_path)
    out_path.write_text(text, encoding="utf-8")
    ui.say("\nRecorded target block:\n")
    ui.say(text)
    ui.say(f"Saved to {out_path}. Paste it into your client config, then run 'audit check --config <config>'.")
    return out_path
