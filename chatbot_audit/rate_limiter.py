"""A simple async rate limiter shared by all conversations in a run."""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Enforce a minimum interval between any two messages sent to the target."""

    def __init__(self, max_messages_per_minute: int) -> None:
        if max_messages_per_minute <= 0:
            raise ValueError("max_messages_per_minute must be greater than 0")
        self.interval = 60.0 / max_messages_per_minute
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def wait(self) -> None:
        """Block until the next message may be sent."""
        async with self._lock:
            delay = self._next_allowed - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_allowed = time.monotonic() + self.interval
