"""All pydantic data models: client config, attacks, and run artifacts."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator

Severity = Literal["critical", "high", "medium", "low"]
Tier = Literal["quick", "standard", "deep"]
Category = Literal["prompt_leak", "false_promises", "ai_disclosure", "data_leak"]
CheckType = Literal[
    "canary_leak",
    "prompt_fragment_leak",
    "forbidden_text",
    "required_text",
    "disallowed_codes",
    "fact_mismatch",
    "pii_leak",
    "ai_disclosure",
    "judge",
]

SEVERITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _compile_regex(value: str, min_groups: int = 0) -> str:
    """Raise ValueError with a readable message if `value` is not a valid regex."""
    try:
        compiled = re.compile(value)
    except re.error as exc:
        raise ValueError(f"invalid regular expression {value!r}: {exc}") from exc
    if compiled.groups < min_groups:
        raise ValueError(f"regular expression {value!r} needs a capture group for the number")
    return value


# ---------------------------------------------------------------- client config


class FactRule(BaseModel):
    name: str
    expected: float
    pattern: str
    context: str
    description: str

    @field_validator("pattern")
    @classmethod
    def _pattern_compiles(cls, value: str) -> str:
        return _compile_regex(value, min_groups=1)

    @field_validator("context")
    @classmethod
    def _context_compiles(cls, value: str) -> str:
        return _compile_regex(value)


class HttpTargetConfig(BaseModel):
    type: Literal["http"]
    url: str
    method: Literal["POST", "GET"] = "POST"
    headers: dict[str, str] = {}
    body: dict[str, Any]
    reply_path: str


class OpenAITargetConfig(BaseModel):
    type: Literal["openai_compatible"]
    base_url: str
    model: str
    api_key_env: str | None = None
    system_prompt_file: str | None = None


WIDGET_SELECTOR_KEYS = (
    "frame", "open_button", "input", "send_button", "bot_message", "typing_indicator", "evidence_area",
)


class WidgetSelectors(BaseModel):
    frame: str | None = None            # CSS selector of the <iframe> that contains the chat, if any
    open_button: str | None = None      # chat bubble/launcher; None if the chat box is already visible
    input: str                          # text box where the customer types
    send_button: str | None = None      # None = press Enter in the input
    bot_message: str                    # matches EVERY bot message element in the conversation
    typing_indicator: str | None = None # element visible while the bot is "typing"
    evidence_area: str | None = None    # element to screenshot as evidence; None = the iframe or chat box


# A bot bubble is skipped when its WHOLE text (trimmed) matches one of these: "…" placeholders and
# short "leave your email" notes. (?=(\S+\s+){0,14}\S+$) means "15 words or fewer", and [^0-9]* lets
# the rest of the note have any words but no numbers, so a bubble with a code or price is never skipped.
DEFAULT_IGNORE_TEXT_PATTERNS = (
    r"\.{3}|…",
    r"(?=(\S+\s+){0,14}\S+$)(please )?leave (us )?your e-?mail\b[^0-9]*",
    r"(?=(\S+\s+){0,14}\S+$)we('ll| will) reply (by|via) e-?mail\b[^0-9]*",
)


class BrowserTargetConfig(BaseModel):
    type: Literal["browser"]
    url: str
    preset: str = "generic"             # name of a file in widget_presets/ (without .yaml)
    selectors: dict[str, str | None] = {}   # overrides merged on top of the preset's selectors
    dismiss_selectors: list[str] = []   # clicked if present after page load (cookie banners etc.)
    reply_settle_ms: int = Field(default=1500, ge=200)   # text must stay unchanged this long
    page_load_timeout_seconds: float = Field(default=30, gt=0)
    headless: bool = True
    record_video: bool = True
    viewport: tuple[int, int] = (1280, 800)
    storage_state_file: str | None = None   # Playwright storage state; relative to the config file
    ignore_text_patterns: list[str] = list(DEFAULT_IGNORE_TEXT_PATTERNS)  # whole-bubble regex, case-insensitive; replaces the defaults

    @field_validator("selectors")
    @classmethod
    def _known_selector_keys(cls, value: dict[str, str | None]) -> dict[str, str | None]:
        unknown = sorted(set(value) - set(WIDGET_SELECTOR_KEYS))
        if unknown:
            raise ValueError(
                f"unknown selector key(s) {', '.join(unknown)}; use {', '.join(WIDGET_SELECTOR_KEYS)}"
            )
        return value

    @field_validator("ignore_text_patterns")
    @classmethod
    def _valid_patterns(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"ignore_text_patterns: '{pattern}' is not a valid regular expression ({exc})") from exc
        return value


class LimitsConfig(BaseModel):
    max_messages_per_minute: int = Field(default=20, gt=0)
    max_parallel_conversations: int = Field(default=2, gt=0)
    timeout_seconds: float = Field(default=30, gt=0)
    retry_pause_seconds: float = Field(default=3, ge=0)


class ClientConfig(BaseModel):
    client: str
    display_name: str
    tier: Tier = "standard"
    target: Annotated[HttpTargetConfig | OpenAITargetConfig | BrowserTargetConfig, Field(discriminator="type")]
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    canary: str | None = None
    prompt_fragments: list[str] = []
    facts: list[FactRule] = []
    allowed_codes: list[str] = []
    known_terms: list[str] = []           # product names etc. that look like codes but never are
    public_contacts: list[str] = []
    disclosure_pattern: str = (
        r"\b(AI|artificial intelligence|virtual assistant|chatbot|automated assistant|bot)\b"
    )
    never_do: list[str] = []
    evidence_target: BrowserTargetConfig | None = None   # used only by `audit replay`

    @field_validator("disclosure_pattern")
    @classmethod
    def _disclosure_compiles(cls, value: str) -> str:
        return _compile_regex(value)


# ---------------------------------------------------------------- attacks


class CheckSpec(BaseModel):
    type: CheckType
    patterns: list[str] = []
    severity: Severity | None = None
    turn: Literal["any", "first", "last"] = "any"
    question: str | None = None

    @field_validator("patterns")
    @classmethod
    def _patterns_compile(cls, value: list[str]) -> list[str]:
        for pattern in value:
            _compile_regex(pattern)
        return value


class Attack(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    title: str
    category: Category
    owasp: str
    severity: Severity
    tiers: list[Tier] = Field(min_length=1)
    turns: list[str] = Field(min_length=1)
    checks: list[CheckSpec] = Field(min_length=1)
    why_it_matters: str
    how_to_fix: str


# ---------------------------------------------------------------- run artifacts


class TurnRecord(BaseModel):
    index: int
    sent: str
    reply: str | None
    latency_ms: int | None
    error: str | None = None
    buttons: list[str] = []      # quick reply button labels shown with the reply (browser targets)


class Exchange(BaseModel):
    run_id: str
    attack_id: str
    status: Literal["completed", "inconclusive"]
    turns: list[TurnRecord]
    error: str | None = None
    started_at: datetime
    finished_at: datetime
    adapter_type: str | None = None
    evidence_video: str | None = None        # relative to the run folder
    evidence_screenshot: str | None = None   # relative to the run folder


class Verdict(BaseModel):
    attack_id: str
    check_type: str
    verdict: Literal["pass", "fail", "inconclusive", "skipped"]
    severity: Severity
    detail: str
    matched_text: str | None = None
    turn_index: int | None = None


class FailedCheck(BaseModel):
    fingerprint: str              # "<attack_id>::<check_type>", kept per check for future retest diffs
    check_type: str
    severity: Severity
    detail: str
    matched_text: str | None
    turn_index: int | None


class Finding(BaseModel):
    """One finding per attack, grouping every check that failed in that conversation."""

    attack_id: str
    severity: Severity            # highest severity among the failed checks
    title: str
    checks: list[FailedCheck] = Field(min_length=1)
    status: Literal["auto_confirmed"] = "auto_confirmed"

    @property
    def fingerprints(self) -> list[str]:
        return [check.fingerprint for check in self.checks]


class RunInfo(BaseModel):
    run_id: str
    client: str
    display_name: str
    tier: str
    adapter_type: str
    attack_count: int
    attack_pack_hash: str
    started_at: datetime
    finished_at: datetime | None = None
    # Added so `evaluate` and `report` can work from the run folder alone.
    config_path: str              # relative to the run folder; see run_folder.resolve_config_path
    attack_pack_dir: str


class Grade(BaseModel):
    score: int
    letter: str | None
    counts_by_severity: dict[str, int]
    passed_tests: int
    total_tests: int
    inconclusive_count: int
    note: str
