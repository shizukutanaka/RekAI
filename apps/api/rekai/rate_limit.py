"""Per-client rate limiting.

Two implementations behind one async interface:

- :class:`LocalRateLimiter` — the in-process token bucket (wraps
  :class:`RateLimiter`). Zero-latency, but each worker/node counts
  independently, so a multi-worker deployment effectively multiplies the limit.
- :class:`RedisRateLimiter` — a fixed-window counter using Redis ``INCR``
  (atomic across workers/nodes), chosen by :func:`build_rate_limiter` when
  ``REKAI_REDIS_URL`` is set. Counting needs atomic increments, which the
  generic ``CacheBackend`` (get/set only) can't provide race-free — hence a
  dedicated Redis client here, mirroring ``metrics_store.py``. If Redis errors
  at runtime the limiter **fails open** (allows the request) so a Redis outage
  degrades to "no rate limiting" rather than "no service".
"""

from __future__ import annotations

import math
import time
from typing import Any, Protocol

from rekai.config import Settings
from rekai.logging_config import get_logger

logger = get_logger("rekai.rate_limit")


class RateLimiter:
    """Fixed-window token bucket.

    Each client gets ``capacity`` tokens that refill over ``window`` seconds.
    Suitable for single-process deployments; use a shared store for multi-node.
    """

    def __init__(self, capacity: int, window: float, max_buckets: int = 10_000) -> None:
        self.capacity = capacity
        self.window = window
        # Soft cap on tracked clients; idle buckets are pruned past this size so
        # a flood of distinct client keys can't grow memory without bound.
        self.max_buckets = max_buckets
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_refill)

    def _tokens_now(self, key: str, now: float) -> tuple[float, float]:
        tokens, last = self._buckets.get(key, (float(self.capacity), now))
        refill = (now - last) * (self.capacity / self.window)
        return min(self.capacity, tokens + refill), last

    def _prune(self, now: float) -> None:
        # Drop buckets that have fully refilled — an idle client is
        # indistinguishable from a brand-new one, so its entry carries no state.
        stale = [k for k in list(self._buckets) if self._tokens_now(k, now)[0] >= self.capacity]
        for k in stale:
            del self._buckets[k]

    def allow(self, key: str) -> bool:
        now = time.time()
        if len(self._buckets) >= self.max_buckets:
            self._prune(now)
        tokens, _ = self._tokens_now(key, now)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        return True

    def remaining(self, key: str) -> int:
        """Whole tokens currently available to ``key`` — a non-consuming peek."""
        tokens, _ = self._tokens_now(key, time.time())
        return int(tokens)

    def retry_after(self, key: str) -> int:
        """Whole seconds until ``key`` has a token again (>= 1; 0 if available now).

        A peek — it does not consume a token — so it is safe to call right after
        ``allow`` returns ``False`` to populate a ``Retry-After`` header.
        """
        now = time.time()
        tokens, _ = self._tokens_now(key, now)
        if tokens >= 1.0:
            return 0
        seconds = (1.0 - tokens) * self.window / self.capacity
        return max(1, math.ceil(seconds))


class AsyncRateLimiter(Protocol):
    """What the request middleware needs from a rate limiter."""

    async def allow(self, key: str) -> bool: ...
    async def remaining(self, key: str) -> int: ...
    async def retry_after(self, key: str) -> int: ...
    @property
    def label(self) -> str: ...


class LocalRateLimiter:
    """Async facade over the in-process token bucket."""

    def __init__(self, capacity: int, window: float) -> None:
        self._limiter = RateLimiter(capacity, window)

    async def allow(self, key: str) -> bool:
        return self._limiter.allow(key)

    async def remaining(self, key: str) -> int:
        return self._limiter.remaining(key)

    async def retry_after(self, key: str) -> int:
        return self._limiter.retry_after(key)

    @property
    def label(self) -> str:
        return "local"


class RedisRateLimiter:
    """Fixed-window counter shared across workers/nodes via Redis ``INCR``.

    The window is identified by ``int(now / window)`` baked into the key, so a
    new window starts atomically for every worker at the same instant; the key
    expires shortly after its window ends to avoid accumulating counters.
    Semantics differ slightly from the local token bucket (counts reset at the
    window edge instead of refilling continuously) — same limit, same headers.
    """

    def __init__(self, url: str, capacity: int, window: float, client: Any = None) -> None:
        if client is None:
            import redis.asyncio as redis  # lazy so redis stays optional

            client = redis.from_url(url, decode_responses=True)
        self._client = client
        self.capacity = capacity
        self.window = window

    def _window_key(self, key: str, now: float) -> str:
        return f"rekai:rl:{key}:{int(now / self.window)}"

    def _seconds_left_in_window(self, now: float) -> int:
        return max(1, math.ceil(self.window - (now % self.window)))

    async def allow(self, key: str) -> bool:
        now = time.time()
        try:
            count = await self._client.incr(self._window_key(key, now))
            if count == 1:
                # Keep the counter one window past its end so a straggling
                # remaining()/retry_after() peek still sees it.
                await self._client.expire(self._window_key(key, now), int(self.window * 2))
            return int(count) <= self.capacity
        except Exception as exc:
            logger.warning("rate limiter failing open (redis error: %s)", exc)
            return True

    async def remaining(self, key: str) -> int:
        now = time.time()
        try:
            raw = await self._client.get(self._window_key(key, now))
        except Exception as exc:
            logger.warning("rate limiter failing open (redis error: %s)", exc)
            return self.capacity
        used = int(raw) if raw else 0
        return max(0, self.capacity - used)

    async def retry_after(self, key: str) -> int:
        # A fixed window admits new requests only when the window rolls over.
        if await self.remaining(key) > 0:
            return 0
        return self._seconds_left_in_window(time.time())

    @property
    def label(self) -> str:
        return "redis"


def build_rate_limiter(settings: Settings) -> AsyncRateLimiter:
    """Redis-shared when ``REKAI_REDIS_URL`` is set, else process-local."""
    if settings.redis_url:
        try:
            return RedisRateLimiter(
                settings.redis_url,
                settings.rate_limit_requests,
                settings.rate_limit_window_seconds,
            )
        except Exception:  # pragma: no cover - fall back if redis client init fails
            pass
    return LocalRateLimiter(settings.rate_limit_requests, settings.rate_limit_window_seconds)
