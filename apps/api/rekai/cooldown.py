"""Per-provider cooldown after a rate limit.

When a provider returns a 429, RekAI parks it for a short while (the upstream
``Retry-After`` when known, else a configured default) so subsequent requests
skip straight to a healthy fallback instead of hammering a provider that has
already told us to back off.

The primary store is process-local (a plain dict) for a zero-latency check on
every attempt. When a shared ``CacheBackend`` is configured (Redis), the
``*_shared`` methods additionally write through to it and consult it on a local
miss — so with ``REKAI_REDIS_URL`` set, a cooldown recorded by one worker/node is
seen by the others; without Redis it degrades to the local-only behaviour.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rekai.cache import CacheBackend

_SHARED_PREFIX = "rekai:cooldown:"


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

    def parked(self) -> dict[str, float]:
        """Currently-parked keys → seconds remaining, expired entries dropped.

        The read side of what ``mark`` records, so ``/health`` can report *which*
        providers are unavailable instead of only counting cooldowns after the
        fact via ``rekai_cooldowns_total``. Local view only: a cooldown another
        worker recorded in Redis needs I/O to see, which ``/health`` deliberately
        doesn't do."""
        now = self._clock()
        for key in [k for k, until in self._until.items() if now >= until]:
            del self._until[key]
        return {key: round(until - now, 1) for key, until in self._until.items()}

    def clear(self) -> None:
        self._until.clear()

    async def mark_shared(self, cache: CacheBackend, key: str, seconds: float) -> None:
        """Like :meth:`mark`, and also write through to ``cache`` (a no-op on a
        non-Redis backend) so other workers/nodes observe the cooldown."""
        self.mark(key, seconds)
        if seconds <= 0:
            return
        await cache.set(_SHARED_PREFIX + key, "1", ttl=max(1, math.ceil(seconds)))

    async def active_shared(self, cache: CacheBackend, key: str) -> bool:
        """Like :meth:`active`, falling back to ``cache`` on a local miss so a
        cooldown recorded by another worker/node is still honoured."""
        if self.active(key):
            return True
        return await cache.get(_SHARED_PREFIX + key) is not None


# Module-level singleton (mirrors rekai.metrics.metrics).
cooldowns = Cooldown()
