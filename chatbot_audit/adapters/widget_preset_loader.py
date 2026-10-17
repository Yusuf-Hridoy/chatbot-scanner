"""Load widget presets and merge them with per-config selector overrides."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from chatbot_audit.config_loader import ConfigError
from chatbot_audit.data_models import WIDGET_SELECTOR_KEYS, BrowserTargetConfig, WidgetSelectors

PRESET_DIR = Path(__file__).resolve().parent / "widget_presets"


def list_presets() -> list[str]:
    """Names of all available presets."""
    return sorted(path.stem for path in PRESET_DIR.glob("*.yaml"))


def load_preset(name: str) -> dict[str, Any]:
    """Read widget_presets/<name>.yaml, failing clearly if it does not exist."""
    path = PRESET_DIR / f"{name}.yaml"
    if not name or "/" in name or "\\" in name or not path.is_file():
        raise ConfigError(f"Unknown widget preset '{name}'. Available presets: {', '.join(list_presets())}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or not isinstance(data.get("selectors"), dict):
        raise ConfigError(f"Widget preset {path.name} must have a 'selectors:' mapping.")
    return data


def resolve_selectors(target: BrowserTargetConfig) -> WidgetSelectors:
    """Preset selectors with the config's `selectors` overrides on top, validated."""
    preset = load_preset(target.preset)
    merged = {key: preset["selectors"].get(key) for key in WIDGET_SELECTOR_KEYS}
    merged.update(target.selectors)
    missing = [key for key in ("input", "bot_message") if not merged.get(key)]
    if missing:
        raise ConfigError(
            f"Selector '{missing[0]}' is required but is empty after merging preset '{target.preset}' "
            "with the config's selectors. Set it under target.selectors or record it with 'audit connect'."
        )
    try:
        return WidgetSelectors.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(f"Invalid selectors for preset '{target.preset}': {exc.errors()[0]['msg']}") from exc


def resolve_dismiss_selectors(target: BrowserTargetConfig) -> list[str]:
    """The preset's dismiss selectors followed by the config's own."""
    preset_dismiss = load_preset(target.preset).get("dismiss_selectors") or []
    return list(dict.fromkeys([*preset_dismiss, *target.dismiss_selectors]))


def is_verified(name: str) -> bool:
    """Whether a preset has been tested against a real widget."""
    return bool(load_preset(name).get("verified", False))


def unverified_preset_warning(target: BrowserTargetConfig) -> str | None:
    """The warning to print when a run or check uses an unverified preset."""
    if is_verified(target.preset):
        return None
    return (
        f"Preset '{target.preset}' is unverified. Run 'audit check' and, if it fails, "
        "use 'audit connect' to record selectors for this site."
    )
