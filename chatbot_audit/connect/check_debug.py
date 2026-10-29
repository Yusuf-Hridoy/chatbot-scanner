"""The `audit check` debug folder: what the harness saw on the page when a browser check failed.

It never saves cookies, local storage or the values of form fields.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Page

from chatbot_audit.connect.selector_recorder import SELECTOR_BUILDER_JS
from chatbot_audit.data_models import WidgetSelectors

if TYPE_CHECKING:
    from chatbot_audit.adapters.base_adapter import AdapterError

DEBUG_FOLDER = "check_debug"
DEBUG_FILES = ("page.png", "frames.json", "candidates.json", "summary.txt")
REVIEWER_NOTE = "Send this folder to your reviewer to fix the selectors."
CANDIDATE_LIMIT = 200
TOP_CANDIDATES = 3
CHILD_LIMIT = 10
MESSAGE_TEXT_HINT = 'To leave out a sender name, set target.selectors.message_text to one of the selectors under "inside".'
SCREENSHOT_TIMEOUT_MS = 10_000

# step -> (what the step does, candidate kind, config key under target.selectors)
STEPS = {
    "page load": ("loading the page", None, None),
    "launcher": ("opening the chat (the launcher button)", "launcher", "open_button"),
    "input": ("finding the message box", "input", "input"),
    "reply": ("reading the bot's reply", "message", "bot_message"),
}
KIND_NAMES = {  # kind -> (one, many)
    "launcher": ("launcher button", "launcher buttons"),
    "input": ("message box", "message boxes"),
    "message": ("bot message element", "bot message elements"),
}
RANK_WORDS = re.compile(r"chat|message|launcher|widget|support|help|bubble|operator|bot|reply", re.IGNORECASE)

# Wrapped in a function so it runs even on pages whose Content Security Policy blocks inline scripts.
_BUILDER_FUNCTION = "() => {\n" + SELECTOR_BUILDER_JS.read_text(encoding="utf-8") + "\n}"

# Likely chat elements in this frame's document and every open shadow root in it. Bot message candidates also
# list the elements inside them, to help pick target.selectors.message_text.
# Inputs never include their value or text: that could be something a person typed.
_CANDIDATES_JS = r"""([limit, childLimit]) => {
  const CLICK_WORDS = /chat|message|launcher|widget|support|help/i;
  const MESSAGE_WORDS = /message|bubble|operator/i;
  const NOT_TEXT_INPUTS = /^(hidden|password|checkbox|radio|submit|button|reset|file|image|range|color)$/i;
  const roots = [document];
  for (let i = 0; i < roots.length; i++) {
    roots[i].querySelectorAll("*").forEach((el) => { if (el.shadowRoot) roots.push(el.shadowRoot); });
  }
  const clean = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const attributes = (el) => {
    const result = {};
    for (const name of ["id", "class", "aria-label", "placeholder", "role"]) {
      const value = el.getAttribute(name);
      if (value) result[name] = clean(value).slice(0, 120);
    }
    for (const attribute of el.attributes) {
      if (attribute.name.startsWith("data-") && attribute.name !== "data-audit-seen") {
        result[attribute.name] = clean(attribute.value).slice(0, 120);
      }
    }
    return result;
  };
  const visible = (el) => {
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return box.width > 0 && box.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  };
  const clickable = (el) =>
    el.matches("button, a[href], [role='button'], [onclick], [tabindex]") || getComputedStyle(el).cursor === "pointer";
  const kindOf = (el, tag) => {
    const hooks = [el.getAttribute("id"), el.getAttribute("class")].join(" ");
    if (tag === "textarea" || (tag === "input" && !NOT_TEXT_INPUTS.test(el.type || "text"))) return "input";
    if (el.hasAttribute("contenteditable") && el.isContentEditable) return "input";
    const words = [(el.innerText || "").slice(0, 200), el.getAttribute("aria-label"), hooks].join(" ");
    if (CLICK_WORDS.test(words) && clickable(el)) return "launcher";
    if (MESSAGE_WORDS.test(hooks)) return "message";
    return null;
  };
  const isTyped = (el) => el.matches("input, textarea, select") || el.isContentEditable;
  const childrenOf = (el) => Array.from(el.querySelectorAll("*")).slice(0, childLimit).map((child) => ({
    tag: child.tagName.toLowerCase(),
    class: clean(child.getAttribute("class")).slice(0, 120),
    selector: window.__auditBuildSelector(child, "inner"),
    text: isTyped(child) ? "" : clean(child.innerText).slice(0, 60),
  }));
  const found = [];
  for (const root of roots) {
    for (const el of root.querySelectorAll("*")) {
      if (found.length >= limit) return found;
      const tag = el.tagName.toLowerCase();
      const kind = kindOf(el, tag);
      if (!kind) continue;
      const item = {
        kind: kind,
        tag: tag,
        selector: window.__auditBuildSelector(el, kind === "message" ? "all_similar" : "unique"),
        text: kind === "input" ? "" : clean(el.innerText).slice(0, 80),
        visible: visible(el),
        in_shadow_dom: el.getRootNode() !== document,
        attributes: attributes(el),
      };
      if (kind === "message") item.children = childrenOf(el);
      found.push(item);
    }
  }
  return found;
}"""

_FRAME_SELECTOR_JS = "el => window.__auditBuildSelector(el, 'unique')"


async def save_debug_folder(
    page: Page, folder: Path, url: str, selectors: WidgetSelectors, error: AdapterError | None
) -> Path:
    """Write page.png, frames.json, candidates.json and summary.txt into `folder` (replacing old ones)."""
    folder = Path(folder)
    if folder.name == DEBUG_FOLDER and folder.is_dir():
        shutil.rmtree(folder)
    folder.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    try:
        await page.screenshot(path=str(folder / "page.png"), full_page=True, timeout=SCREENSHOT_TIMEOUT_MS)
    except PlaywrightError as exc:
        notes.append(f"The screenshot could not be saved: {_first_line(exc)}")

    frames: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for frame in page.frames:
        frames.append({"url": frame.url, "name": frame.name or None, "iframe_selector": await _iframe_selector(frame)})
        try:
            await frame.evaluate(_BUILDER_FUNCTION)
            found = await frame.evaluate(_CANDIDATES_JS, [CANDIDATE_LIMIT, CHILD_LIMIT])
        except PlaywrightError as exc:
            notes.append(f"Could not look inside the frame {frame.url}: {_first_line(exc)}")
            continue
        candidates += [{"frame_url": frame.url, **item} for item in found]

    _write_json(folder / "frames.json", frames)
    _write_json(folder / "candidates.json", candidates)
    (folder / "summary.txt").write_text(_summary(url, selectors, error, candidates, notes), encoding="utf-8")
    return folder


async def _iframe_selector(frame: Frame) -> str | None:
    """Selector of the <iframe> element in the parent page (None for the main page)."""
    if frame.parent_frame is None:
        return None
    try:
        await frame.parent_frame.evaluate(_BUILDER_FUNCTION)
        element = await frame.frame_element()
        return await element.evaluate(_FRAME_SELECTOR_JS)
    except PlaywrightError:
        return None


def _first_line(exc: Exception) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else exc.__class__.__name__


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def rank_candidates(candidates: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    """The most likely candidates of one kind: visible ones first, then those with chat-like words."""

    def score(item: dict[str, Any]) -> int:
        words = " ".join([item["text"], *item["attributes"].values()])
        points = 10 if item["visible"] else 0
        points += 2 * min(len(RANK_WORDS.findall(words)), 5)
        if kind == "input" and item["tag"] != "input":
            points += 1  # chat boxes are usually a textarea or a contenteditable div
        return points

    pool = [item for item in candidates if item["kind"] == kind]
    return sorted(pool, key=score, reverse=True)[:TOP_CANDIDATES]  # stable: page order breaks ties


def _describe(number: int, item: dict[str, Any]) -> list[str]:
    details = [item["tag"], "visible" if item["visible"] else "hidden"]
    if item["in_shadow_dom"]:
        details.append("inside a shadow root")
    lines = [f"  {number}. {item['selector']}", f"     {', '.join(details)}; frame: {item['frame_url']}"]
    if item["text"]:
        lines.append(f'     text: "{item["text"]}"')
    if item["attributes"]:
        attributes = ", ".join(f'{name}="{value}"' for name, value in item["attributes"].items())
        lines.append(f"     attributes: {attributes}")
    if item.get("children"):
        lines.append("     inside:")
        for child in item["children"]:
            details = child["tag"] + (f', class "{child["class"]}"' if child["class"] else "")
            text = f': "{child["text"]}"' if child["text"] else ""
            lines.append(f"       - {child['selector']} ({details}){text}")
    return lines


def _candidate_block(candidates: list[dict[str, Any]], kind: str) -> list[str]:
    ranked = rank_candidates(candidates, kind)
    one, many = KIND_NAMES[kind]
    if not ranked:
        lines = [f"No {many} found."]
    elif len(ranked) == 1:
        lines = [f"The most likely {one}:"]
    else:
        lines = [f"The {len(ranked)} most likely {many}:"]
    for number, item in enumerate(ranked, start=1):
        lines += _describe(number, item)
    if any(item.get("children") for item in ranked):
        lines.append(MESSAGE_TEXT_HINT)
    return lines


def _summary(
    url: str,
    selectors: WidgetSelectors,
    error: AdapterError | None,
    candidates: list[dict[str, Any]],
    notes: list[str],
) -> str:
    lines = ["audit check: debug summary", "", f"Page: {url}", "Selectors used:"]
    lines += [f"  {key}: {value}" for key, value in selectors.model_dump().items() if value]
    lines.append("")
    step = getattr(error, "step", None) if error is not None else None
    if error is None:
        lines += ["Result: the check worked. This folder was saved because you used --debug.", ""]
        for kind in ("launcher", "input", "message"):
            lines += _candidate_block(candidates, kind) + [""]
    elif step in STEPS:
        doing, kind, key = STEPS[step]
        lines += [
            f'Result: the check failed at the "{step}" step ({doing}).',
            f"What happened: {error}",
            f"Selector used: {error.selector or 'none'}",
            "",
        ]
        if kind is None:
            lines += ["The page did not load, so there are no chat elements to suggest.", ""]
        else:
            lines += _candidate_block(candidates, kind)
            lines += ["", f"Try one of these in your config under target.selectors.{key}, then run 'audit check' again.", ""]
    else:
        lines += [f"Result: the check failed. What happened: {error}", ""]
    if notes:
        lines += ["Notes:", *[f"  - {note}" for note in notes], ""]
    lines += [
        "Files in this folder:",
        "  page.png          a screenshot of the whole page",
        "  frames.json       every frame on the page, with the selector of its iframe",
        "  candidates.json   likely chat elements in every frame and shadow root",
        "  summary.txt       this file",
        "",
        "No cookies, stored data or typed text are saved here.",
        "",
    ]
    return "\n".join(lines)
