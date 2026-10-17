"""OpenAI-compatible chat completions adapter; keeps conversation history client-side."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import httpx

from chatbot_audit.adapters.base_adapter import AdapterError, BaseAdapter, BotReply, post_json
from chatbot_audit.adapters.http_adapter import _MISSING, resolve_path
from chatbot_audit.config_loader import ConfigError
from chatbot_audit.data_models import LimitsConfig, OpenAITargetConfig


class OpenAICompatibleAdapter(BaseAdapter):
    """POSTs the full message history to `{base_url}/chat/completions` on every turn."""

    adapter_type = "openai_compatible"

    def __init__(self, target: OpenAITargetConfig, limits: LimitsConfig) -> None:
        self.target = target
        self.timeout = limits.timeout_seconds
        self.url = target.base_url.rstrip("/") + "/chat/completions"
        headers: dict[str, str] = {}
        if target.api_key_env:
            key = os.environ.get(target.api_key_env)
            if not key:
                raise ConfigError(
                    f"Environment variable {target.api_key_env} is required by the config (target.api_key_env)"
                )
            headers["Authorization"] = f"Bearer {key}"
        self.system_prompt = (
            Path(target.system_prompt_file).read_text(encoding="utf-8") if target.system_prompt_file else None
        )
        self.client = httpx.AsyncClient(timeout=limits.timeout_seconds, headers=headers)
        self.histories: dict[str, list[dict[str, str]]] = {}

    async def start_conversation(self) -> str:
        conversation_id = str(uuid.uuid4())
        self.histories[conversation_id] = (
            [{"role": "system", "content": self.system_prompt}] if self.system_prompt else []
        )
        return conversation_id

    async def send(self, conversation_id: str, message: str) -> BotReply:
        history = self.histories[conversation_id]
        user_message = {"role": "user", "content": message}
        payload = {"model": self.target.model, "messages": [*history, user_message]}
        started = time.perf_counter()
        data = await post_json(self.client, "POST", self.url, self.timeout, json=payload)
        latency_ms = int((time.perf_counter() - started) * 1000)
        reply = resolve_path(data, "choices.0.message.content")
        if reply is _MISSING or reply is None:
            raise AdapterError("No reply found at choices[0].message.content in the chatbot's response.")
        text = reply if isinstance(reply, str) else str(reply)
        # Only record the turn once it succeeded, so a retry resends cleanly.
        history.extend([user_message, {"role": "assistant", "content": text}])
        return BotReply(text=text, latency_ms=latency_ms, raw=data)

    async def close(self) -> None:
        await self.client.aclose()
