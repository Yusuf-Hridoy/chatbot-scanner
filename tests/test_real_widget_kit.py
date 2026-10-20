"""The human test kit for a real Crisp widget (examples/real_widget_test/)."""

import re

from chatbot_audit.config_loader import load_client_config
from chatbot_audit.data_models import BrowserTargetConfig
from conftest import REPO_ROOT

KIT = REPO_ROOT / "examples" / "real_widget_test"


def test_kit_config_loads() -> None:
    config = load_client_config(KIT / "client_config.yaml")
    assert isinstance(config.target, BrowserTargetConfig)
    assert config.target.preset == "crisp" and config.target.headless is False
    assert config.target.url == "http://localhost:8000/"
    assert (config.limits.max_parallel_conversations, config.limits.timeout_seconds) == (1, 90)


def test_kit_page_has_placeholder_and_no_real_ids() -> None:
    html = (KIT / "index.html").read_text(encoding="utf-8")
    assert "<!-- Paste your Crisp chatbox script here -->" in html
    assert "CRISP_WEBSITE_ID" not in html
    assert "<script" not in html.lower()
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", html)  # no website ID


def test_how_to_test_lists_the_steps() -> None:
    steps = (KIT / "HOW_TO_TEST.md").read_text(encoding="utf-8")
    for text in (
        "Settings → Workspace Settings → Setup & Integrations → HTML",
        "python -m http.server 8000",
        "audit check --config client_config.yaml",
        "Hi, I'm the Lumen AI assistant.",
        "audit check --config client_config.yaml --debug",
        "audit connect --url http://localhost:8000/",
        "check_debug/",
        "target_recorded.yaml",
    ):
        assert text in steps, text
