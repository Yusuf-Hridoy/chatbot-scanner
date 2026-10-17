from collections import Counter
from pathlib import Path

import pytest

from chatbot_audit.attacks.attack_loader import DEFAULT_PACK_DIR, filter_by_tier, load_attacks
from chatbot_audit.config_loader import ConfigError

ATTACK_TEMPLATE = """
  - id: {id}
    title: A test attack
    category: prompt_leak
    owasp: LLM07
    severity: high
    tiers: [standard]
    turns: ["hello"]
    checks:
      - {{type: canary_leak}}
    why_it_matters: Because.
    how_to_fix: Fix it.
"""


def test_thirty_attacks_load() -> None:
    attacks = load_attacks()
    assert len(attacks) == 30
    assert Counter(a.category for a in attacks) == {
        "prompt_leak": 10,
        "false_promises": 10,
        "ai_disclosure": 4,
        "data_leak": 6,
    }


def test_attack_ids_unique() -> None:
    ids = [attack.id for attack in load_attacks()]
    assert len(ids) == len(set(ids))


def test_tier_filter() -> None:
    attacks = load_attacks()
    assert len(filter_by_tier(attacks, "quick")) == 12
    assert len(filter_by_tier(attacks, "standard")) == 30
    assert len(filter_by_tier(attacks, "deep")) == 30


def test_every_attack_has_customer_text() -> None:
    for attack in load_attacks():
        assert attack.title and attack.why_it_matters and attack.how_to_fix


def test_duplicate_ids_fail_clearly(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("attacks:" + ATTACK_TEMPLATE.format(id="same-id"), encoding="utf-8")
    (tmp_path / "b.yaml").write_text("attacks:" + ATTACK_TEMPLATE.format(id="same-id"), encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_attacks(tmp_path)
    assert "Duplicate attack id 'same-id'" in str(excinfo.value)
    assert "b.yaml" in str(excinfo.value) and "a.yaml" in str(excinfo.value)


def test_invalid_regex_in_attack_names_file_and_field(tmp_path: Path) -> None:
    text = (
        "attacks:" + ATTACK_TEMPLATE.format(id="bad-regex").replace(
            "- {type: canary_leak}", "- {type: forbidden_text, patterns: ['(unclosed']}"
        )
    )
    (tmp_path / "broken.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_attacks(tmp_path)
    assert "broken.yaml" in str(excinfo.value)
    assert "checks.0.patterns" in str(excinfo.value)


def test_default_pack_dir_has_four_files() -> None:
    assert sorted(p.name for p in DEFAULT_PACK_DIR.glob("*.yaml")) == [
        "ai_disclosure.yaml",
        "data_leak.yaml",
        "false_promises.yaml",
        "prompt_leak.yaml",
    ]
