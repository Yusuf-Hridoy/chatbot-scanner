"""Load and validate attack packs, and filter them by tier."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from pydantic import ValidationError

from chatbot_audit.config_loader import ConfigError, format_validation_error
from chatbot_audit.data_models import Attack

DEFAULT_PACK_DIR = Path(__file__).parent / "attack_packs"


def pack_files(pack_dir: Path = DEFAULT_PACK_DIR) -> list[Path]:
    """All attack pack files in a folder, in a stable order."""
    files = sorted(Path(pack_dir).glob("*.yaml"))
    if not files:
        raise ConfigError(f"No attack pack files (*.yaml) found in {pack_dir}")
    return files


def load_attacks(pack_dir: Path = DEFAULT_PACK_DIR) -> list[Attack]:
    """Load every attack in `pack_dir`; fail clearly on invalid fields or duplicate IDs."""
    attacks: list[Attack] = []
    seen: dict[str, str] = {}
    for path in pack_files(pack_dir):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc
        items = raw.get("attacks") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            raise ConfigError(f"{path.name} must have a top-level 'attacks:' list.")
        for index, item in enumerate(items):
            label = f"attacks.{index}"
            if isinstance(item, dict) and item.get("id"):
                label = f"attacks.{index} ({item['id']})"
            try:
                attack = Attack.model_validate(item)
            except ValidationError as exc:
                raise ConfigError(format_validation_error(path.name, exc, prefix=label)) from exc
            if attack.id in seen:
                raise ConfigError(
                    f"Duplicate attack id '{attack.id}' in {path.name} (already defined in {seen[attack.id]})."
                )
            seen[attack.id] = path.name
            attacks.append(attack)
    return attacks


def filter_by_tier(attacks: list[Attack], tier: str) -> list[Attack]:
    """Attacks that belong to the given tier."""
    return [attack for attack in attacks if tier in attack.tiers]


def attack_pack_hash(pack_dir: Path = DEFAULT_PACK_DIR) -> str:
    """sha256 of the concatenated attack pack files, for traceability."""
    digest = hashlib.sha256()
    for path in pack_files(pack_dir):
        digest.update(path.read_bytes())
    return digest.hexdigest()
