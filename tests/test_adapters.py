import pytest

from chatbot_audit.adapters.base_adapter import AdapterError, BaseAdapter, BotReply, create_adapter
from chatbot_audit.adapters.http_adapter import _MISSING, HttpAdapter, fill_template, resolve_path
from chatbot_audit.adapters.openai_compatible_adapter import OpenAICompatibleAdapter
from chatbot_audit.config_loader import load_client_config
from chatbot_audit.data_models import CheckSpec
from chatbot_audit.pipeline.step1_run_attacks import run_conversation
from chatbot_audit.rate_limiter import RateLimiter
from conftest import demo_config, make_attack


def test_resolve_path_nested_and_indexes() -> None:
    data = {"choices": [{"message": {"content": "hi"}}], "data": {"reply": "ok"}}
    assert resolve_path(data, "choices.0.message.content") == "hi"
    assert resolve_path(data, "$.data.reply") == "ok"
    assert resolve_path(data, "choices.5.message") is _MISSING
    assert resolve_path(data, "data.missing") is _MISSING


def test_fill_template_recurses() -> None:
    body = {"a": "{{message}}", "b": [{"s": "id={{session}}"}], "n": 3}
    assert fill_template(body, "hello", "abc") == {"a": "hello", "b": [{"s": "id=abc"}], "n": 3}


async def test_http_adapter_round_trip(unsafe_bot, make_config) -> None:
    adapter = create_adapter(load_client_config(make_config(unsafe_bot)))
    assert isinstance(adapter, HttpAdapter)
    try:
        conversation = await adapter.start_conversation()
        reply = await adapter.send(conversation, "What's the minimum order for free shipping?")
    finally:
        await adapter.close()
    assert reply.text == "Free shipping on orders over €50."
    assert reply.latency_ms >= 0
    assert reply.raw == {"data": {"reply": reply.text}}


async def test_http_adapter_keeps_server_side_session(safe_bot, make_config) -> None:
    adapter = create_adapter(load_client_config(make_config(safe_bot)))
    try:
        conversation = await adapter.start_conversation()
        first = await adapter.send(conversation, "Hi")
        second = await adapter.send(conversation, "Hi")
    finally:
        await adapter.close()
    assert first.text.startswith("Hi, I'm Lumen's AI assistant.")
    assert not second.text.startswith("Hi, I'm Lumen's AI assistant.")


async def test_http_adapter_missing_reply_path(unsafe_bot, make_config) -> None:
    config = load_client_config(make_config(unsafe_bot))
    config.target.reply_path = "data.nope"
    adapter = create_adapter(config)
    try:
        with pytest.raises(AdapterError, match="No reply found at reply_path 'data.nope'"):
            await adapter.send(await adapter.start_conversation(), "Hi")
    finally:
        await adapter.close()


async def test_openai_adapter_keeps_history(safe_bot, make_config) -> None:
    adapter = create_adapter(load_client_config(make_config(safe_bot, kind="openai")))
    assert isinstance(adapter, OpenAICompatibleAdapter)
    try:
        conversation = await adapter.start_conversation()
        first = await adapter.send(conversation, "Hi")
        second = await adapter.send(conversation, "How many days do I have to return a product?")
    finally:
        await adapter.close()
    history = adapter.histories[conversation]
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    assert history[1]["content"] == first.text
    # The bot only adds its disclosure on the first message, so the history really was sent.
    assert first.text.startswith("Hi, I'm Lumen's AI assistant.")
    assert second.text == "You can return any product within 14 days for a full refund."


async def test_simulated_error_raises_adapter_error(unsafe_bot, make_config) -> None:
    for kind in ("http", "openai"):
        adapter = create_adapter(load_client_config(make_config(unsafe_bot, kind=kind)))
        try:
            with pytest.raises(AdapterError, match="HTTP 500"):
                await adapter.send(await adapter.start_conversation(), "__simulate_error__")
        finally:
            await adapter.close()


async def test_unreachable_target_raises_adapter_error(make_config, unsafe_bot) -> None:
    config = load_client_config(make_config(unsafe_bot))
    config.target.url = "http://127.0.0.1:1/chat"
    adapter = create_adapter(config)
    try:
        with pytest.raises(AdapterError, match="Could not reach the chatbot"):
            await adapter.send(await adapter.start_conversation(), "Hi")
    finally:
        await adapter.close()


# Phase 1.1 fix 6: retry only on timeouts, connection errors and 5xx; fail at once on 4xx.
async def test_http_4xx_is_not_retryable(unsafe_bot, make_config) -> None:
    config = load_client_config(make_config(unsafe_bot))
    config.target.url = config.target.url.replace("/chat", "/no-such-path")  # demo bot answers 404
    adapter = create_adapter(config)
    try:
        with pytest.raises(AdapterError, match="HTTP 404") as excinfo:
            await adapter.send(await adapter.start_conversation(), "Hi")
    finally:
        await adapter.close()
    assert excinfo.value.retryable is False


async def test_5xx_and_connection_errors_are_retryable(unsafe_bot, make_config) -> None:
    adapter = create_adapter(load_client_config(make_config(unsafe_bot)))
    try:
        with pytest.raises(AdapterError) as excinfo:
            await adapter.send(await adapter.start_conversation(), "__simulate_error__")
    finally:
        await adapter.close()
    assert excinfo.value.retryable is True

    config = load_client_config(make_config(unsafe_bot))
    config.target.url = "http://127.0.0.1:1/chat"
    adapter = create_adapter(config)
    try:
        with pytest.raises(AdapterError) as excinfo:
            await adapter.send(await adapter.start_conversation(), "Hi")
    finally:
        await adapter.close()
    assert excinfo.value.retryable is True


class FailingAdapter(BaseAdapter):
    """Fails every send with the given error and counts the attempts."""

    def __init__(self, error: AdapterError) -> None:
        self.error = error
        self.calls = 0

    async def start_conversation(self) -> str:
        return "conversation"

    async def send(self, conversation_id: str, message: str) -> BotReply:
        self.calls += 1
        raise self.error

    async def close(self) -> None:
        pass


@pytest.mark.parametrize(
    ("error", "expected_calls"),
    [
        (AdapterError("The chatbot returned HTTP 400.", retryable=False), 1),
        (AdapterError("The chatbot returned HTTP 503.", retryable=True), 2),
        (AdapterError("The chatbot did not answer within 5 seconds.", retryable=True), 2),
    ],
)
async def test_run_conversation_retries_only_retryable_errors(error: AdapterError, expected_calls: int) -> None:
    config = demo_config(limits={"max_messages_per_minute": 6000, "retry_pause_seconds": 0})
    adapter = FailingAdapter(error)
    attack = make_attack(CheckSpec(type="pii_leak"), turns=["one", "two"])
    exchange = await run_conversation(attack, adapter, RateLimiter(6000), config, "r")
    assert adapter.calls == expected_calls
    assert exchange.status == "inconclusive"
    assert exchange.error == str(error)
    assert [t.reply for t in exchange.turns] == [None, None]
