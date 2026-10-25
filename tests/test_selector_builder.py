"""selector_builder.js on static pages, and the `audit connect` recorder driven by scripted clicks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import yaml
from playwright.async_api import Browser, Page, async_playwright

from chatbot_audit.adapters.browser_adapter import BrowserAdapter
from chatbot_audit.connect.selector_recorder import (
    CLOSE_POPUPS_PROMPT,
    SELECTOR_BUILDER_JS,
    SelectorRecorder,
    target_yaml,
)
from chatbot_audit.data_models import BrowserTargetConfig, LimitsConfig
from conftest import bot_port

pytestmark = pytest.mark.browser

STATIC_PAGE = """
<main>
  <button id="send-button" class="btn">Send</button>
  <button id="ember4821" data-testid="close" class="btn primary-button">Close</button>
  <div class="css-1x2y3z sc-bZkfAO message-box">Box</div>
  <input name="email" placeholder="Email">
  <span>one</span><span>two</span>
  <ul class="thread">
    <li class="message bot-msg css-9f8e7d" data-message-id="m-101">Hi there</li>
    <li class="message user-msg css-1a2b3c" data-message-id="m-102">Hello</li>
    <li class="message bot-msg css-4d5e6f" data-message-id="m-103">How can I help?</li>
    <li class="message bot-msg css-7a8b9c" data-message-id="m-104">Anything else?</li>
  </ul>
</main>
"""


@pytest_asyncio.fixture
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        yield browser
        await browser.close()


@pytest_asyncio.fixture
async def static_page(browser: Browser) -> AsyncIterator[Page]:
    page = await browser.new_page()
    await page.set_content(STATIC_PAGE)
    await page.add_script_tag(path=str(SELECTOR_BUILDER_JS))
    yield page
    await page.close()


async def build(page: Page, selector: str, mode: str = "unique") -> str:
    return await page.locator(selector).first.evaluate(
        "(el, mode) => window.__auditBuildSelector(el, mode)", mode
    )


async def count(page: Page, selector: str) -> int:
    return await page.locator(selector).count()


async def test_never_builds_a_selector_for_html_or_body(static_page: Page) -> None:
    await static_page.evaluate("() => document.body.classList.add('page')")
    for tag in ("html", "body"):
        for mode in ("unique", "all_similar", "textbox"):
            assert await build(static_page, tag, mode) is None, (tag, mode)
    # An element with no hooks of its own does not climb up to the body's class.
    await static_page.evaluate("() => document.body.insertAdjacentHTML('beforeend', '<p id=plain>Plain</p>')")
    assert await build(static_page, "body > p", "all_similar") == "p"


async def test_unique_prefers_stable_id(static_page: Page) -> None:
    assert await build(static_page, "text=Send") == "#send-button"


async def test_unique_prefers_data_testid_over_classes_and_unstable_id(static_page: Page) -> None:
    assert await build(static_page, "text=Close") == '[data-testid="close"]'


async def test_unique_skips_generated_classes(static_page: Page) -> None:
    assert await build(static_page, "text=Box") == "div.message-box"


@pytest.mark.parametrize("target", ["text=Send", "text=Close", "text=Box", "input", "text=two", "text=Hi there"])
async def test_unique_matches_exactly_one(static_page: Page, target: str) -> None:
    selector = await build(static_page, target)
    assert await count(static_page, selector) == 1
    same = await static_page.locator(target).first.evaluate(
        "(el, sel) => document.querySelector(sel) === el", selector
    )
    assert same, f"{selector} matched a different element"


async def test_all_similar_matches_every_bot_message(static_page: Page) -> None:
    selector = await build(static_page, "text=How can I help?", "all_similar")
    assert selector == "li.message.bot-msg"
    assert await count(static_page, selector) == 3
    assert ":nth" not in selector


async def test_all_similar_walks_up_to_an_ancestor_with_hooks(static_page: Page) -> None:
    await static_page.set_content('<div class="reply" data-author="bot"><p><b>Deep</b> text</p></div>')
    await static_page.add_script_tag(path=str(SELECTOR_BUILDER_JS))
    assert await build(static_page, "b", "all_similar") == 'div[data-author="bot"]'


SHADOW_PAGE = """
<lumen-chat></lumen-chat>
<script>
  const root = document.querySelector("lumen-chat").attachShadow({ mode: "open" });
  root.innerHTML = '<div class="panel"><div class="msg bot">Hi</div><div class="msg bot">Hello again</div>'
    + '<textarea class="box" aria-label="Message"></textarea></div>';
</script>
"""


async def test_selectors_inside_an_open_shadow_root(static_page: Page) -> None:
    await static_page.set_content(SHADOW_PAGE)
    await static_page.add_script_tag(path=str(SELECTOR_BUILDER_JS))
    unique = await build(static_page, "textarea")  # Playwright CSS reaches into the shadow root
    assert unique == '[aria-label="Message"]'
    assert await count(static_page, unique) == 1
    similar = await build(static_page, "text=Hello again", "all_similar")
    assert similar == "div.msg.bot"
    assert await count(static_page, similar) == 2


# ---------------------------------------------------------------- recorder flow (scripted clicks)


class ScriptedUI:
    """Plays the person at the terminal: performs real clicks on the page when prompted."""

    def __init__(self) -> None:
        self.page: Page | None = None
        self.lines: list[str] = []
        self.prompts: list[str] = []
        self.tasks: list[asyncio.Task] = []
        self.banner_closed_by_click = False

    async def _click_when_picking(self, locator) -> None:
        """Click only once the recorder has switched pick mode on, like a person reading the prompt."""
        for _ in range(200):
            modes = []
            for frame in self.page.frames:
                try:
                    modes.append(await frame.evaluate("() => window.__auditPickMode"))
                except Exception:  # noqa: BLE001 - frame still loading
                    pass
            if any(modes):
                break
            await asyncio.sleep(0.05)
        await locator.click()

    def say(self, text: str) -> None:
        self.lines.append(text)
        frame = self.page.frame_locator("iframe[data-chat-frame]") if self.page else None
        if text.startswith("Click inside the message box"):
            self.tasks.append(asyncio.ensure_future(self._click_when_picking(frame.locator("textarea"))))
        elif text.startswith("Click on the bot's reply"):
            bubble = frame.locator(".bot-message").first
            self.tasks.append(asyncio.ensure_future(self._click_when_picking(bubble)))

    async def wait_for_enter(self, prompt: str) -> None:
        self.prompts.append(prompt)
        if prompt.startswith("Close any cookie banners"):
            # Pick mode is off: this click must accept the cookies, not be recorded.
            await self.page.locator("[data-cookie-accept]").click()
            self.banner_closed_by_click = not await self.page.locator("[data-cookie-banner]").is_visible()
        elif prompt.startswith("Click the chat bubble"):
            await self.page.locator("[data-chat-launcher]").click()
            await asyncio.sleep(1.0)  # the recorder sees the click first, then this "Enter"
        # Any other prompt (typing indicator): press Enter to skip.

    async def confirm(self, question: str) -> bool:
        return True


async def test_recorder_flow_produces_working_selectors(browser: Browser, unsafe_bot) -> None:
    url = f"http://127.0.0.1:{bot_port(unsafe_bot)}/"
    context = await browser.new_context()
    ui = ScriptedUI()
    recorder = await SelectorRecorder.attach(context, ui, pick_timeout=20)
    ui.page = recorder.page
    try:
        target = await recorder.record(url)
    finally:
        await context.close()

    assert ui.prompts[0] == CLOSE_POPUPS_PROMPT
    assert ui.banner_closed_by_click
    assert target["selectors"] == {
        "frame": "[data-chat-frame]",
        "open_button": "[data-chat-launcher]",
        "input": "[data-chat-input]",
        "send_button": None,
        "bot_message": 'div[data-chat-role="bot"]',
        "typing_indicator": None,
    }
    assert yaml.safe_load(target_yaml(target))["target"] == target

    # The recorded block works for a real conversation (what `audit check` does).
    adapter = BrowserAdapter(
        BrowserTargetConfig.model_validate({**target, "reply_settle_ms": 600, "record_video": False}),
        LimitsConfig(timeout_seconds=15),
    )
    try:
        reply = await adapter.send(await adapter.start_conversation(), "Hello")
    finally:
        await adapter.close()
    assert reply.text == "Hello! How can I help you today?"
