from pathlib import Path

import pytest
import yaml

from chatbot_audit.config_loader import ConfigError, config_summary, load_client_config
from chatbot_audit.data_models import HttpTargetConfig, OpenAITargetConfig
from conftest import HTTP_EXAMPLE, OPENAI_EXAMPLE


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "client_config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _example() -> dict:
    return yaml.safe_load(HTTP_EXAMPLE.read_text(encoding="utf-8"))


def test_http_example_loads() -> None:
    config = load_client_config(HTTP_EXAMPLE)
    assert isinstance(config.target, HttpTargetConfig)
    assert config.canary == "CANARY-DEMO-7F3K"
    assert config.allowed_codes == ["WELCOME10"]
    assert [fact.name for fact in config.facts] == ["refund_window", "free_shipping_threshold"]


def test_openai_example_loads() -> None:
    config = load_client_config(OPENAI_EXAMPLE)
    assert isinstance(config.target, OpenAITargetConfig)
    assert config.target.model == "demo"


def test_missing_env_var_gives_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEMO_TOKEN", raising=False)
    data = _example()
    data["target"]["headers"] = {"Authorization": "Bearer {{env.DEMO_TOKEN}}"}
    with pytest.raises(ConfigError) as excinfo:
        load_client_config(_write(tmp_path, data))
    assert str(excinfo.value) == (
        "Environment variable DEMO_TOKEN is required by client_config.yaml (target.headers.Authorization)"
    )


def test_env_var_is_substituted_and_masked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_TOKEN", "super-secret-value")
    data = _example()
    data["target"]["headers"] = {"Authorization": "Bearer {{env.DEMO_TOKEN}}"}
    config = load_client_config(_write(tmp_path, data))
    assert config.target.headers["Authorization"] == "Bearer super-secret-value"
    assert "super-secret-value" not in str(config_summary(config))
    # Per-request placeholders are left alone for the adapter.
    assert config.target.body["message"] == "{{message}}"


def test_invalid_regex_names_the_field(tmp_path: Path) -> None:
    data = _example()
    data["facts"][0]["pattern"] = "(\\d+"
    with pytest.raises(ConfigError) as excinfo:
        load_client_config(_write(tmp_path, data))
    message = str(excinfo.value)
    assert "client_config.yaml" in message
    assert "facts.0.pattern" in message


def test_zero_rate_limit_fails(tmp_path: Path) -> None:
    data = _example()
    data["limits"]["max_messages_per_minute"] = 0
    with pytest.raises(ConfigError) as excinfo:
        load_client_config(_write(tmp_path, data))
    assert "limits.max_messages_per_minute" in str(excinfo.value)


def test_unknown_target_type_fails(tmp_path: Path) -> None:
    data = _example()
    data["target"]["type"] = "carrier_pigeon"
    with pytest.raises(ConfigError):
        load_client_config(_write(tmp_path, data))


def test_system_prompt_file_resolved_relative_to_config(tmp_path: Path) -> None:
    (tmp_path / "prompt.txt").write_text("You are a test bot.", encoding="utf-8")
    data = yaml.safe_load(OPENAI_EXAMPLE.read_text(encoding="utf-8"))
    data["target"]["system_prompt_file"] = "prompt.txt"
    config = load_client_config(_write(tmp_path, data))
    assert Path(config.target.system_prompt_file) == (tmp_path / "prompt.txt").resolve()
