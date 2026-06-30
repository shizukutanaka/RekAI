"""Per-provider cooldown after a rate limit.

When a provider returns a 429, RekAI parks it for a short while (the upstream
``Retry-After`` when known, else a configured default) so subsequent requests
skip straight to a healthy fallback instead of hammering a provider that has
already told us to back off. Process-local — like the rate limiter and metrics.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class Cooldown:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._until: dict[str, float] = {}
        self._clock = clock

    def mark(self, key: str, seconds: float) -> None:
        """Park ``key`` for ``seconds`` (extending an existing, later cooldown)."""
        if seconds <= 0:
            return
        until = self._clock() + seconds
        self._until[key] = max(self._until.get(key, 0.0), until)

    def active(self, key: str) -> bool:
        until = self._until.get(key)
        if until is None:
            return False
        if self._clock() >= until:
            del self._until[key]  # expired — clean up
            return False
        return True

    def remaining(self, key: str) -> float:
        until = self._until.get(key)
        return max(0.0, until - self._clock()) if until is not None else 0.0

    def clear(self) -> None:
        self._until.clear()


# Module-level singleton (mirrors rekai.metrics.metrics).
cooldowns = Cooldown()
