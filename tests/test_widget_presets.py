"""Widget presets, browser target config and model compatibility (no browser needed)."""

from pathlib import Path

import pytest
import yaml

from chatbot_audit.adapters.browser_adapter import BrowserAdapter
from chatbot_audit.adapters.widget_preset_loader import (
    is_verified,
    list_presets,
    load_preset,
    resolve_chat_dismiss_selectors,
    resolve_dismiss_selectors,
    resolve_selectors,
    unverified_preset_warning,
)
from chatbot_audit.config_loader import ConfigError, load_client_config
from chatbot_audit.data_models import BrowserTargetConfig, ClientConfig, Exchange, LimitsConfig
from chatbot_audit.pipeline.run_folder import read_jsonl
from chatbot_audit.pipeline.step1_run_attacks import effective_parallelism
from conftest import BROWSER_EXAMPLE, demo_config


def target(**fields) -> BrowserTargetConfig:
    return BrowserTargetConfig.model_validate({"type": "browser", "url": "http://127.0.0.1:8765/", **fields})


def test_presets_load() -> None:
    assert list_presets() == ["crisp", "generic", "intercom", "tidio"]
    for name in list_presets():
        preset = load_preset(name)
        assert preset["name"] == name
        assert preset["selectors"]["input"] and preset["selectors"]["bot_message"]


def test_unknown_preset_lists_available() -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_preset("zendesk")
    assert "Unknown widget preset 'zendesk'" in str(excinfo.value)
    assert "crisp, generic, intercom, tidio" in str(excinfo.value)


def test_overrides_merge_on_top_of_preset() -> None:
    selectors = resolve_selectors(
        target(selectors={"frame": "iframe[data-chat-frame]", "typing_indicator": None, "input": "#box"})
    )
    generic = load_preset("generic")["selectors"]
    assert selectors.frame == "iframe[data-chat-frame]"
    assert selectors.typing_indicator is None
    assert selectors.input == "#box"
    assert selectors.bot_message == generic["bot_message"]  # untouched keys come from the preset
    assert selectors.open_button == generic["open_button"]


def test_evidence_area_is_optional_and_overridable() -> None:
    assert resolve_selectors(target()).evidence_area is None
    assert resolve_selectors(target(selectors={"evidence_area": "#chat-box"})).evidence_area == "#chat-box"


def test_missing_input_after_merge_fails_clearly() -> None:
    with pytest.raises(ConfigError) as excinfo:
        resolve_selectors(target(selectors={"input": None}))
    message = str(excinfo.value)
    assert "'input'" in message and "preset 'generic'" in message and "audit connect" in message


def test_unknown_selector_key_rejected() -> None:
    with pytest.raises(ValueError, match="unknown selector key"):
        target(selectors={"launcher": "#x"})


def test_verified_flag_and_warning() -> None:
    assert is_verified("generic")
    assert not any(is_verified(name) for name in ("intercom", "tidio", "crisp"))
    assert unverified_preset_warning(target()) is None
    assert unverified_preset_warning(target(preset="intercom")) == (
        "Preset 'intercom' is unverified. Run 'audit check' and, if it fails, "
        "use 'audit connect' to record selectors for this site."
    )


def test_dismiss_selectors_combine_preset_and_config() -> None:
    selectors = resolve_dismiss_selectors(target(dismiss_selectors=["#close-promo"]))
    assert selectors == [load_preset("generic")["dismiss_selectors"][0], "#close-promo"]


def test_chat_dismiss_selectors_combine_preset_and_config() -> None:
    assert resolve_chat_dismiss_selectors(target()) == []
    selectors = resolve_chat_dismiss_selectors(target(preset="crisp", chat_dismiss_selectors=["#close-survey"]))
    assert selectors == ["button:has-text('Skip')", "#close-survey"]


def test_message_text_is_an_optional_selector() -> None:
    assert resolve_selectors(target()).message_text is None
    assert resolve_selectors(target(selectors={"message_text": ".text"})).message_text == ".text"


def test_crisp_preset_uses_the_selectors_that_worked_on_a_real_widget() -> None:
    preset = load_preset("crisp")
    assert preset["selectors"]["input"] == "textarea[name='message']"
    assert preset["selectors"]["bot_message"] == "[class*='--operator'][class*='--text']"
    assert preset["chat_dismiss_selectors"] == ["button:has-text('Skip')"]
    assert preset["verified"] is False
    assert "sender-name handling still needs one real check" in preset["description"]


def test_browser_example_config_loads() -> None:
    config = load_client_config(BROWSER_EXAMPLE)
    assert isinstance(config.target, BrowserTargetConfig)
    assert config.target.selectors == {"frame": "iframe[data-chat-frame]"}
    assert config.limits.max_parallel_conversations == 3


def _write_config(tmp_path: Path, **target_fields) -> Path:
    data = yaml.safe_load(BROWSER_EXAMPLE.read_text(encoding="utf-8"))
    data["target"].update(target_fields)
    path = tmp_path / "client_config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_storage_state_file_resolved_or_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="storage_state_file points to a missing file"):
        load_client_config(_write_config(tmp_path, storage_state_file="state.json"))
    (tmp_path / "state.json").write_text("{}", encoding="utf-8")
    config = load_client_config(_write_config(tmp_path, storage_state_file="state.json"))
    assert Path(config.target.storage_state_file) == (tmp_path / "state.json").resolve()


def test_config_with_incomplete_selectors_fails_at_load(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_client_config(_write_config(tmp_path, selectors={"bot_message": None}))
    assert "client_config.yaml: target:" in str(excinfo.value)
    assert "'bot_message'" in str(excinfo.value)


def test_browser_parallelism_capped_at_three() -> None:
    notes: list[str] = []
    browser = ClientConfig.model_validate(
        {**demo_config().model_dump(), "target": target().model_dump(), "limits": {"max_parallel_conversations": 8}}
    )
    assert effective_parallelism(browser, notes.append) == 3
    assert notes and "at most 3" in notes[0]
    assert effective_parallelism(demo_config(limits={"max_parallel_conversations": 8}), notes.append) == 8


def test_old_exchange_lines_still_load(tmp_path: Path) -> None:
    old_line = (
        '{"run_id": "r", "attack_id": "a", "status": "completed", "turns": [{"index": 0, "sent": "hi", '
        '"reply": "hello", "latency_ms": 5, "error": null}], "error": null, '
        '"started_at": "2026-10-17T10:00:00Z", "finished_at": "2026-10-17T10:00:01Z"}\n'
    )
    path = tmp_path / "exchanges.jsonl"
    path.write_text(old_line, encoding="utf-8")
    [exchange] = read_jsonl(path, Exchange)
    assert exchange.adapter_type is None
    assert exchange.evidence_video is None and exchange.evidence_screenshot is None


def test_ignore_text_patterns_defaults_and_overrides() -> None:
    ignored = BrowserAdapter(target(record_video=False), LimitsConfig())._ignored

    for text in (
        "...",
        " … ",
        "Leave your email so we can reply.",
        "Leave us your e-mail",
        "We'll reply by email.",
        "Leave your email so we can reply if you leave the chat.",
    ):
        assert ignored(text), text
    for text in (
        "Free shipping on orders over €50.",
        "You can also email support@lumen-skincare.example.",
        "Hello!",
        "Loading...",
        # The whole bubble must match: a reply that only contains the phrase is never skipped.
        "Sure! Leave your email and I'll send you a 50% code SORRY50.",
        # A bubble with a number in it (a code, a price) is never skipped.
        "Leave your email and get code SAVE20.",
        # The email patterns only skip short bubbles (15 words or fewer).
        "Leave your email and we will tell you about every new cream, serum and mask the day it comes out.",
    ):
        assert not ignored(text), text
    assert target(ignore_text_patterns=["^beep$"]).ignore_text_patterns == ["^beep$"]  # replaces the defaults
    custom = BrowserAdapter(target(record_video=False, ignore_text_patterns=["beep"]), LimitsConfig())._ignored
    assert custom("BEEP") and not custom("beep beep")  # case-insensitive, whole bubble


def test_invalid_ignore_pattern_rejected() -> None:
    with pytest.raises(ValueError, match="not a valid regular expression"):
        target(ignore_text_patterns=["(unclosed"])
