"""Generic HTTP adapter: fills a JSON body template and reads the reply at a dotted path."""

from __future__ import annotations

import copy
import time
import uuid
from typing import Any

import httpx

from chatbot_audit.adapters.base_adapter import AdapterError, BaseAdapter, BotReply, post_json
from chatbot_audit.data_models import HttpTargetConfig, LimitsConfig

_MISSING = object()


def fill_template(value: Any, message: str, session: str) -> Any:
    """Replace {{message}} and {{session}} in every string, recursively."""
    if isinstance(value, dict):
        return {k: fill_template(v, message, session) for k, v in value.items()}
    if isinstance(value, list):
        return [fill_template(v, message, session) for v in value]
    if isinstance(value, str):
        # Session first, so a message that contains "{{session}}" is sent verbatim.
        return value.replace("{{session}}", session).replace("{{message}}", message)
    return value


def resolve_path(data: Any, path: str) -> Any:
    """Follow a dotted path like `choices.0.message.content`; return _MISSING if absent."""
    if path.startswith("$."):
        path = path[2:]
    current = data
    for part in path.split(".") if path else []:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.lstrip("-").isdigit() and -len(current) <= int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
    return current


class HttpAdapter(BaseAdapter):
    """Talks to any JSON-over-HTTP chat endpoint that keeps session state server-side."""

    def __init__(self, target: HttpTargetConfig, limits: LimitsConfig) -> None:
        self.target = target
        self.timeout = limits.timeout_seconds
        self.client = httpx.AsyncClient(timeout=limits.timeout_seconds, headers=target.headers)

    async def start_conversation(self) -> str:
        return str(uuid.uuid4())

    async def send(self, conversation_id: str, message: str) -> BotReply:
        body = fill_template(copy.deepcopy(self.target.body), message, conversation_id)
        kwargs: dict[str, Any] = {"params": body} if self.target.method == "GET" else {"json": body}
        started = time.perf_counter()
        data = await post_json(self.client, self.target.method, self.target.url, self.timeout, **kwargs)
        latency_ms = int((time.perf_counter() - started) * 1000)
        reply = resolve_path(data, self.target.reply_path)
        if reply is _MISSING or reply is None:
            raise AdapterError(
                f"No reply found at reply_path '{self.target.reply_path}' in the chatbot's response."
            )
        return BotReply(text=reply if isinstance(reply, str) else str(reply), latency_ms=latency_ms, raw=data)

    async def close(self) -> None:
        await self.client.aclose()
