"""Browser adapter: chats through a website's chat widget in a real Chromium browser."""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, FrameLocator, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from chatbot_audit.adapters.base_adapter import AdapterError, BaseAdapter, BotReply, ConversationEvidence
from chatbot_audit.adapters.widget_preset_loader import resolve_dismiss_selectors, resolve_selectors
from chatbot_audit.data_models import BrowserTargetConfig, LimitsConfig

CAPTCHA_RE = re.compile(r"recaptcha|hcaptcha|challenges\.cloudflare\.com|turnstile", re.IGNORECASE)
CAPTCHA_MESSAGE = (
    "This page shows a CAPTCHA or bot check. Ask the customer for a staging URL or to allowlist your IP. "
    "The harness will not try to bypass it."
)
CHROMIUM_MISSING = "Chromium could not be started. Install it with: playwright install chromium"
DISMISS_WAIT_SECONDS = 2.0
APPEAR_POLL_SECONDS = 0.1
SETTLE_POLL_SECONDS = 0.25
IFRAME_SOURCES_JS = "() => Array.from(document.querySelectorAll('iframe')).map(f => f.getAttribute('src') || '')"
# Closest ancestor of the input that also contains a bot message (null if there is none below <body>).
CHAT_BOX_JS = """(input, botSelector) => {
  for (let node = input.parentElement; node && node !== document.body; node = node.parentElement) {
    if (node.querySelector(botSelector)) return node;
  }
  return null;
}"""
PAGE_SCREENSHOT_SUFFIX = ".page.png"
AREA_SCREENSHOT_TIMEOUT_MS = 5000
# Bot messages already on the page before a send get this mark; a new message is one without it.
SEEN_ATTRIBUTE = "data-audit-seen"
MARK_SEEN_JS = "(elements, mark) => elements.forEach(el => el.setAttribute(mark, '1'))"
# [already seen?, text, button labels] per bot message. Buttons inside a bubble (quick replies) are kept
# apart from the text, and an element inside another matched element is skipped (a selector list can match both).
READ_BUBBLES_JS = r"""(elements, mark) => elements.filter(
  el => !elements.some(other => other !== el && other.contains(el))
).map(el => {
  const controls = Array.from(el.querySelectorAll("button, [role='button']"));
  let text = el.innerText;
  if (controls.length) {
    const copy = el.cloneNode(true);
    copy.querySelectorAll("button, [role='button']").forEach(button => button.remove());
    text = copy.textContent.replace(/\s+/g, " ");
  }
  const labels = controls
    .map(button => (button.innerText || button.textContent || "").replace(/\s+/g, " ").trim())
    .filter(label => label);
  return [el.hasAttribute(mark), (text || "").trim(), labels];
})"""

Scope = Page | FrameLocator


@dataclass
class _Conversation:
    context: BrowserContext
    page: Page
    scope: Scope


def _first_line(exc: Exception) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else exc.__class__.__name__


class BrowserAdapter(BaseAdapter):
    """One Chromium per adapter; one fresh browser context (cookies, storage, video) per conversation."""

    adapter_type = "browser"

    def __init__(self, target: BrowserTargetConfig, limits: LimitsConfig) -> None:
        self.target = target
        self.timeout = limits.timeout_seconds
        self.selectors = resolve_selectors(target)
        self.dismiss_selectors = resolve_dismiss_selectors(target)
        self.ignore_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in target.ignore_text_patterns]
        self.debug_dir: Path | None = None      # set by `audit check`: where a failure saves its debug folder
        self.debug_folder: Path | None = None   # the debug folder once it is written
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._launch_lock = asyncio.Lock()
        self._conversations: dict[str, _Conversation] = {}
        self._video_root = Path(tempfile.mkdtemp(prefix="audit-video-")) if target.record_video else None

    # ------------------------------------------------------------ lifecycle

    async def _ensure_browser(self) -> Browser:
        async with self._launch_lock:
            if self._browser is None:
                try:
                    self._playwright = await async_playwright().start()
                    self._browser = await self._playwright.chromium.launch(headless=self.target.headless)
                except PlaywrightError as exc:
                    raise AdapterError(f"{CHROMIUM_MISSING} ({_first_line(exc)})") from exc
            return self._browser

    async def start_conversation(self) -> str:
        browser = await self._ensure_browser()
        options: dict[str, Any] = {
            "viewport": {"width": self.target.viewport[0], "height": self.target.viewport[1]},
        }
        if self.target.storage_state_file:
            options["storage_state"] = self.target.storage_state_file
        if self._video_root is not None:
            options["record_video_dir"] = str(self._video_root / uuid.uuid4().hex)
            options["record_video_size"] = options["viewport"]
        context = await browser.new_context(**options)
        page: Page | None = None
        try:
            page = await context.new_page()
            await self._load_page(page)
            await self._check_for_captcha(page)
            await self._dismiss_overlays(page)
            scope: Scope = page.frame_locator(self.selectors.frame) if self.selectors.frame else page
            await self._open_chat(page, scope)
            await self._wait_for_quiet(scope)
        except BaseException as exc:
            if isinstance(exc, AdapterError) and page is not None:
                await self._save_debug(page, exc)
            await context.close()
            raise
        conversation_id = str(uuid.uuid4())
        self._conversations[conversation_id] = _Conversation(context=context, page=page, scope=scope)
        return conversation_id

    async def _load_page(self, page: Page) -> None:
        try:
            await page.goto(self.target.url, timeout=self.target.page_load_timeout_seconds * 1000)
        except PlaywrightError as exc:
            raise AdapterError(
                f"The page {self.target.url} did not load: {_first_line(exc)}",
                retryable=True,
                step="page load",
                selector=self.target.url,
            ) from exc

    async def _check_for_captcha(self, page: Page) -> None:
        """Stop (without retrying) if the page or any of its frames embeds a CAPTCHA/bot check."""
        for frame in page.frames:
            sources = [frame.url]
            try:
                sources += await frame.evaluate(IFRAME_SOURCES_JS)
            except PlaywrightError:
                pass  # detached or cross-origin frame: its URL is still checked
            if any(CAPTCHA_RE.search(source or "") for source in sources):
                raise AdapterError(CAPTCHA_MESSAGE, step="page load", selector=self.target.url)

    async def _dismiss_overlays(self, page: Page) -> None:
        """Click cookie banners and similar overlays that become visible within 2 seconds."""
        pending = list(self.dismiss_selectors)
        deadline = time.monotonic() + DISMISS_WAIT_SECONDS
        while pending and time.monotonic() < deadline:
            for selector in list(pending):
                locator = page.locator(selector).first
                try:
                    if await locator.is_visible():
                        await locator.click(timeout=2000)
                        pending.remove(selector)
                except PlaywrightError:
                    pending.remove(selector)  # invalid selector or element vanished: ignore it
            await asyncio.sleep(APPEAR_POLL_SECONDS)

    async def _open_chat(self, page: Page, scope: Scope) -> None:
        """Wait for the input; click the launcher first if the chat box is closed.

        Widgets often load late, so this waits up to page_load_timeout_seconds.
        """
        input_locator = scope.locator(self.selectors.input).first
        open_selector = self.selectors.open_button
        wait_seconds = self.target.page_load_timeout_seconds
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if await self._visible(input_locator):
                return
            if open_selector:
                launcher = await self._find_visible(open_selector, [page, scope])
                if launcher is not None:
                    try:
                        await launcher.click(timeout=5000)
                    except PlaywrightError as exc:
                        raise AdapterError(
                            f"Could not click the chat launcher '{open_selector}': {_first_line(exc)}. "
                            "Run 'audit connect' to record selectors for this site.",
                            step="launcher",
                            selector=open_selector,
                        ) from exc
                    await self._wait_visible(input_locator, deadline)
                    return
            await asyncio.sleep(APPEAR_POLL_SECONDS * 2)
        searched = f"input '{self.selectors.input}'"
        if open_selector:
            searched += f" or open button '{open_selector}'"
        raise AdapterError(
            f"Could not find the chat {searched} within {wait_seconds:g} seconds. "
            "Run 'audit connect' to record selectors for this site.",
            step="launcher" if open_selector else "input",
            selector=open_selector or self.selectors.input,
        )

    async def _wait_visible(self, locator: Any, deadline: float) -> None:
        while time.monotonic() < deadline:
            if await self._visible(locator):
                return
            await asyncio.sleep(APPEAR_POLL_SECONDS)
        raise AdapterError(
            f"The chat input '{self.selectors.input}' did not appear after opening the chat. "
            "Run 'audit connect' to record selectors for this site.",
            step="input",
            selector=self.selectors.input,
        )

    async def _wait_for_quiet(self, scope: Scope) -> None:
        """Let messages that arrive as the chat opens (welcome messages) finish before the first send.

        Waits until the bot messages stay unchanged for reply_settle_ms.
        """
        bot_messages = scope.locator(self.selectors.bot_message)
        settle_seconds = self.target.reply_settle_ms / 1000
        deadline = time.monotonic() + self.target.page_load_timeout_seconds
        last: list[str] | None = None
        stable_since = time.monotonic()
        while time.monotonic() < deadline:
            try:
                texts = await bot_messages.all_inner_texts()
            except PlaywrightError:
                return
            now = time.monotonic()
            if texts != last:
                last, stable_since = texts, now
            elif now - stable_since >= settle_seconds:
                return
            await asyncio.sleep(SETTLE_POLL_SECONDS)

    @staticmethod
    async def _visible(locator: Any) -> bool:
        try:
            return await locator.is_visible()
        except PlaywrightError:
            return False

    async def _find_visible(self, selector: str, scopes: list[Scope]) -> Any | None:
        for scope in scopes:
            locator = scope.locator(selector).first
            if await self._visible(locator):
                return locator
        return None

    # ------------------------------------------------------------ messages

    async def send(self, conversation_id: str, message: str) -> BotReply:
        conversation = self._conversations[conversation_id]
        try:
            return await self._send(conversation, message)
        except AdapterError as exc:
            await self._save_debug(conversation.page, exc)
            raise

    async def _send(self, conversation: _Conversation, message: str) -> BotReply:
        scope = conversation.scope
        bot_messages = scope.locator(self.selectors.bot_message)
        try:
            await bot_messages.evaluate_all(MARK_SEEN_JS, SEEN_ATTRIBUTE)
            input_locator = scope.locator(self.selectors.input).first
            await input_locator.fill(message, timeout=self.timeout * 1000)
            if self.selectors.send_button:
                await scope.locator(self.selectors.send_button).first.click(timeout=self.timeout * 1000)
            else:
                await input_locator.press("Enter")
        except PlaywrightError as exc:
            raise AdapterError(
                f"Could not type into the chat input '{self.selectors.input}': {_first_line(exc)}. "
                "Run 'audit connect' to record selectors for this site.",
                step="input",
                selector=self.selectors.input,
            ) from exc
        submitted = time.monotonic()
        try:
            return await self._read_reply(scope, bot_messages, submitted)
        except PlaywrightError as exc:
            raise AdapterError(
                f"The chat page stopped responding: {_first_line(exc)}",
                retryable=True,
                step="reply",
                selector=self.selectors.bot_message,
            ) from exc

    def _ignored(self, text: str) -> bool:
        """True if the whole bubble text matches an ignore_text_patterns entry."""
        text = text.strip()
        return any(pattern.fullmatch(text) for pattern in self.ignore_patterns)

    async def _new_bot_messages(self, bot_messages: Any) -> tuple[list[str], list[str], int]:
        """Texts and button labels of bot messages that were not on the page before this send
        (minus ignored ones), and how many new message elements there are in total."""
        bubbles = await bot_messages.evaluate_all(READ_BUBBLES_JS, SEEN_ATTRIBUTE)
        new = [(text, labels) for seen, text, labels in bubbles if not seen]
        kept = [(text, labels) for text, labels in new if (text or labels) and not self._ignored(text)]
        texts = [text for text, _ in kept if text]
        buttons = [label for _, labels in kept for label in labels]
        return texts, buttons, len(new)

    async def _read_reply(self, scope: Scope, bot_messages: Any, submitted: float) -> BotReply:
        """Wait for new bot messages, the typing indicator to go away, and the text to settle."""
        deadline = submitted + self.timeout
        timeout_error = AdapterError(
            f"The bot did not reply within {self.timeout:g} seconds.",
            retryable=True,
            step="reply",
            selector=self.selectors.bot_message,
        )

        while not (await self._new_bot_messages(bot_messages))[0]:
            if time.monotonic() > deadline:
                raise timeout_error
            await asyncio.sleep(APPEAR_POLL_SECONDS)
        latency_ms = int((time.monotonic() - submitted) * 1000)

        if self.selectors.typing_indicator:
            typing = scope.locator(self.selectors.typing_indicator).first
            while await self._visible(typing) and time.monotonic() < deadline:
                await asyncio.sleep(APPEAR_POLL_SECONDS)

        settle_seconds = self.target.reply_settle_ms / 1000
        last_text, last_buttons, last_count, stable_since = "", [], -1, time.monotonic()
        while True:
            texts, buttons, count = await self._new_bot_messages(bot_messages)
            text = "\n".join(texts)
            now = time.monotonic()
            if text != last_text or buttons != last_buttons or count != last_count:
                last_text, last_buttons, last_count, stable_since = text, buttons, count, now
            elif text and now - stable_since >= settle_seconds:
                break
            if now > deadline:
                if last_text:
                    break  # partial reply: keep what arrived rather than failing
                raise timeout_error
            await asyncio.sleep(SETTLE_POLL_SECONDS)
        return BotReply(text=last_text.strip(), latency_ms=latency_ms, buttons=last_buttons)

    # ------------------------------------------------------------ audit check debug folder

    async def _save_debug(self, page: Page, error: AdapterError | None) -> None:
        """Write the debug folder once, if `audit check` asked for one. Never hides the original error."""
        if self.debug_dir is None or self.debug_folder is not None:
            return
        from chatbot_audit.connect.check_debug import save_debug_folder

        self.debug_folder = await save_debug_folder(page, self.debug_dir, self.target.url, self.selectors, error)

    async def save_debug(self, conversation_id: str) -> Path | None:
        """Write the debug folder for a working conversation (`audit check --debug`)."""
        conversation = self._conversations.get(conversation_id)
        if conversation is not None:
            await self._save_debug(conversation.page, None)
        return self.debug_folder

    # ------------------------------------------------------------ evidence + cleanup

    async def end_conversation(
        self, conversation_id: str, evidence_dir: Path | None, evidence_name: str
    ) -> ConversationEvidence:
        """Save the chat-area and full-page screenshots and the video (if recorded), then close the context."""
        conversation = self._conversations.pop(conversation_id, None)
        evidence = ConversationEvidence()
        if conversation is None:
            return evidence
        page = conversation.page
        try:
            if evidence_dir is not None:
                evidence_dir.mkdir(parents=True, exist_ok=True)
                await self._save_screenshots(conversation, evidence_dir, evidence_name, evidence)
        finally:
            await conversation.context.close()  # finalizes the video file
        video = page.video
        if video is not None:
            try:
                if evidence_dir is not None:
                    target = evidence_dir / f"{evidence_name}.webm"
                    target.unlink(missing_ok=True)  # a resumed attack overwrites its old evidence
                    await video.save_as(str(target))
                    evidence.video_path = target
                await video.delete()
            except PlaywrightError as exc:
                evidence.warnings.append(f"Video not saved: {_first_line(exc)}")
        return evidence

    async def _save_screenshots(
        self, conversation: _Conversation, evidence_dir: Path, evidence_name: str, evidence: ConversationEvidence
    ) -> None:
        """<name>.page.png is the full page; <name>.png (used by the report) is the chat area only."""
        page = conversation.page
        page_shot = evidence_dir / f"{evidence_name}{PAGE_SCREENSHOT_SUFFIX}"
        screenshot = evidence_dir / f"{evidence_name}.png"
        try:
            await page.screenshot(path=str(page_shot), full_page=True)
        except PlaywrightError as exc:
            evidence.warnings.append(f"Screenshot not saved: {_first_line(exc)}")
            return
        saved = False
        try:
            area = await self._evidence_area(conversation)
            if area is not None:
                await area.screenshot(path=str(screenshot), timeout=AREA_SCREENSHOT_TIMEOUT_MS)
                saved = True
        except PlaywrightError:
            pass  # chat area hidden, detached or not matched
        if not saved:
            shutil.copyfile(page_shot, screenshot)  # fall back to the full page
        evidence.screenshot_path = screenshot

    async def _evidence_area(self, conversation: _Conversation) -> Any | None:
        """The element to screenshot: evidence_area, else the chat iframe, else the input's chat box."""
        page, scope = conversation.page, conversation.scope
        if self.selectors.evidence_area:
            return await self._find_visible(self.selectors.evidence_area, [scope, page])
        if self.selectors.frame:
            return page.locator(self.selectors.frame).first
        handle = await scope.locator(self.selectors.input).first.evaluate_handle(
            CHAT_BOX_JS, self.selectors.bot_message, timeout=AREA_SCREENSHOT_TIMEOUT_MS
        )
        return handle.as_element()

    async def close(self) -> None:
        for conversation_id in list(self._conversations):
            conversation = self._conversations.pop(conversation_id)
            try:
                await conversation.context.close()
            except PlaywrightError:
                pass
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        if self._video_root is not None:
            shutil.rmtree(self._video_root, ignore_errors=True)
