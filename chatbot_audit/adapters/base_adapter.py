"""Abstract adapter interface and the adapter factory."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import httpx
from pydantic import BaseModel

from chatbot_audit.data_models import ClientConfig, HttpTargetConfig


class AdapterError(Exception):
    """Any failure talking to the chatbot: network, timeout, bad status, missing reply.

    `retryable` is True only for timeouts, connection errors and HTTP 5xx responses.
    """

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class BotReply(BaseModel):
    text: str
    latency_ms: int
    raw: Any | None = None


class BaseAdapter(ABC):
    """Interface every target adapter implements (HTTP, OpenAI-compatible, later browser)."""

    @abstractmethod
    async def start_conversation(self) -> str:
        """Open a new conversation and return its id."""

    @abstractmethod
    async def send(self, conversation_id: str, message: str) -> BotReply:
        """Send one user message and return the bot's reply."""

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
    return OpenAICompatibleAdapter(config.target, config.limits)
