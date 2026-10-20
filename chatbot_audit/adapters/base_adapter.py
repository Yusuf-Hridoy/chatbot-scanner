"""Abstract adapter interface and the adapter factory."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from chatbot_audit.data_models import BrowserTargetConfig, ClientConfig, HttpTargetConfig


class AdapterError(Exception):
    """Any failure talking to the chatbot: network, timeout, bad status, missing reply.

    `retryable` is True only for timeouts, connection errors and HTTP 5xx responses.
    Browser errors also name the `step` that failed ("page load", "launcher", "input" or "reply")
    and the `selector` used for it, for the `audit check` debug folder.
    """

    def __init__(
        self, message: str, retryable: bool = False, step: str | None = None, selector: str | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.step = step
        self.selector = selector


class BotReply(BaseModel):
    text: str
    latency_ms: int
    raw: Any | None = None
    buttons: list[str] = []       # quick reply button labels inside the reply (browser adapter only)


class ConversationEvidence(BaseModel):
    video_path: Path | None = None
    screenshot_path: Path | None = None
    warnings: list[str] = []      # evidence problems worth telling the user; never fatal


class BaseAdapter(ABC):
    """Interface every target adapter implements (HTTP, OpenAI-compatible, browser)."""

    adapter_type: str | None = None

    @abstractmethod
    async def start_conversation(self) -> str:
        """Open a new conversation and return its id."""

    @abstractmethod
    async def send(self, conversation_id: str, message: str) -> BotReply:
        """Send one user message and return the bot's reply."""

    async def end_conversation(
        self, conversation_id: str, evidence_dir: Path | None, evidence_name: str
    ) -> ConversationEvidence:
        """Close one conversation. Adapters that record evidence save it into evidence_dir as <evidence_name>.webm/.png."""
        return ConversationEvidence()  # default: nothing to save (HTTP/OpenAI adapters)

    @abstractmethod
    async def close(self) -> None:
        """Release network resources."""


async def post_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    timeout_seconds: float,
    **kwargs: Any,
) -> Any:
    """Make one request and return parsed JSON, converting every failure to AdapterError."""
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        raise AdapterError(
            f"The chatbot at {url} did not answer within {timeout_seconds:g} seconds.", retryable=True
        ) from exc
    except httpx.RequestError as exc:
        raise AdapterError(
            f"Could not reach the chatbot at {url}: {exc.__class__.__name__}.", retryable=True
        ) from exc
    if not response.is_success:
        raise AdapterError(
            f"The chatbot at {url} returned HTTP {response.status_code}.",
            retryable=response.status_code >= 500,
        )
    try:
        return response.json()
    except ValueError as exc:
        raise AdapterError(f"The chatbot at {url} did not return valid JSON.") from exc


def create_adapter(config: ClientConfig) -> BaseAdapter:
    """Build the adapter that matches `config.target.type`."""
    from chatbot_audit.adapters.http_adapter import HttpAdapter
    from chatbot_audit.adapters.openai_compatible_adapter import OpenAICompatibleAdapter

    if isinstance(config.target, HttpTargetConfig):
        return HttpAdapter(config.target, config.limits)
    if isinstance(config.target, BrowserTargetConfig):
        from chatbot_audit.adapters.browser_adapter import BrowserAdapter

        return BrowserAdapter(config.target, config.limits)
    return OpenAICompatibleAdapter(config.target, config.limits)
