"""Load and validate a client config, with {{env.NAME}} substitution."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import ValidationError

from chatbot_audit.data_models import ClientConfig, HttpTargetConfig

ENV_PATTERN = re.compile(r"\{\{\s*env\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class ConfigError(Exception):
    """A user-facing problem with a config file or attack pack."""


def format_validation_error(file_name: str, exc: ValidationError, prefix: str = "") -> str:
    """Turn a pydantic ValidationError into one readable line per problem."""
    lines = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in (prefix, *err["loc"]) if part != "")
        lines.append(f"{file_name}: {loc or '(top level)'}: {err['msg']}")
    return "\n".join(lines)


def _substitute_env(value: Any, file_name: str, path: str) -> Any:
    """Recursively replace {{env.NAME}} in every string of a YAML structure."""
    if isinstance(value, dict):
        return {k: _substitute_env(v, file_name, f"{path}.{k}" if path else str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v, file_name, f"{path}.{i}") for i, v in enumerate(value)]
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ConfigError(f"Environment variable {name} is required by {file_name} ({path})")
            return os.environ[name]

        return ENV_PATTERN.sub(replace, value)
    return value


def load_client_config(path: Path) -> ClientConfig:
    """Read, substitute env vars, and validate a client_config.yaml."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name} must contain a YAML mapping at the top level.")

    raw = _substitute_env(raw, path.name, "")
    try:
        config = ClientConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(format_validation_error(path.name, exc)) from exc

    target = config.target
    if not isinstance(target, HttpTargetConfig) and target.system_prompt_file:
        prompt_path = (path.parent / target.system_prompt_file).resolve()
        if not prompt_path.is_file():
            raise ConfigError(
                f"{path.name}: target.system_prompt_file points to a missing file: {prompt_path}"
            )
        target.system_prompt_file = str(prompt_path)
    return config


def _mask_url(url: str) -> str:
    parts = urlsplit(url)
    shown = f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}{parts.path}"
    return shown + ("?***" if parts.query else "")


def config_summary(config: ClientConfig) -> dict[str, Any]:
    """A printable summary of a config with all secret values masked."""
    target = config.target
    if isinstance(target, HttpTargetConfig):
        target_info: dict[str, Any] = {
            "url": _mask_url(target.url),
            "method": target.method,
            "headers": {name: "***" for name in target.headers},
            "reply_path": target.reply_path,
        }
    else:
        target_info = {
            "base_url": _mask_url(target.base_url),
            "model": target.model,
            "api_key": f"from env {target.api_key_env} (***)" if target.api_key_env else "none",
            "system_prompt_file": target.system_prompt_file or "none",
        }
    return {
        "client": config.client,
        "display_name": config.display_name,
        "adapter_type": target.type,
        "tier": config.tier,
        "target": target_info,
        "limits": config.limits.model_dump(),
        "canary": "configured" if config.canary else "none",
        "prompt_fragments": len(config.prompt_fragments),
        "facts": [fact.name for fact in config.facts],
        "allowed_codes": config.allowed_codes,
    }
