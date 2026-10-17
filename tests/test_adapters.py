import pytest

from chatbot_audit.adapters.base_adapter import AdapterError, create_adapter
from chatbot_audit.adapters.http_adapter import _MISSING, HttpAdapter, fill_template, resolve_path
from chatbot_audit.adapters.openai_compatible_adapter import OpenAICompatibleAdapter
from chatbot_audit.config_loader import load_client_config


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
