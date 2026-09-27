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

# Share of the bucket cap reclaimed in one eviction pass. Evicting a single
# entry per admission would leave every request at the cap paying a full O(n)
# scan; taking a batch amortizes that scan across the admissions that follow.
_EVICT_FRACTION = 0.1


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
        # key -> (tokens, last_refill, capacity) — capacity rides with the
        # bucket because per-key overrides (REKAI_CLIENT_RATE_LIMITS) make it
        # key-specific; each key only ever sees its own, so consistency holds.
        self._buckets: dict[str, tuple[float, float, int]] = {}

    def _cap(self, capacity: int | None) -> int:
        return self.capacity if capacity is None else capacity

    def _tokens_now(self, key: str, now: float, capacity: int | None) -> tuple[float, float]:
        cap = self._cap(capacity)
        tokens, last, _ = self._buckets.get(key, (float(cap), now, cap))
        refill = (now - last) * (cap / self.window)
        return min(cap, tokens + refill), last

    def _reclaim(self, now: float) -> None:
        """Bring the bucket count back under ``max_buckets``.

        Fully-refilled buckets go first: an idle client is indistinguishable
        from a brand-new one, so its entry carries no state and dropping it is
        free. Reclaiming *only* those was the whole policy, and it silently
        failed in the case it exists for — a flood of distinct client ids, where
        every bucket is mid-refill and therefore unprunable. The dict then grew
        past the cap without limit, and because the scan ran on every subsequent
        request the limiter degraded quadratically (measured: 8000 distinct ids
        against the default 60-per-60s config took 4.4s and left 1612 buckets
        under a 1000 cap). That turns the component meant to *stop* abuse into
        an algorithmic-complexity amplifier for it.

        So when that isn't enough, evict the buckets **closest to full**. The
        order is a security property, not tidiness: evicting a bucket resets its
        client to full capacity, so eviction hands budget back. Discarding the
        least-throttled clients gives away the least — and never rewards the
        most-throttled, which the opposite policy would, letting an attacker
        flood distinct keys to force their own exhausted bucket out and reset
        their limit.

        Eviction is batched so the O(n) pass is amortized over the next
        ``max_buckets * _EVICT_FRACTION`` admissions instead of being repeated
        per request.
        """
        # "Full" and "closest to full" are measured per bucket's own capacity —
        # with per-key overrides an absolute-token comparison would misjudge a
        # cap-5 bucket at 4 tokens as healthier than a cap-60 bucket at 40.
        levels = [
            (self._tokens_now(k, now, cap)[0] / cap, k) for k, (_, _, cap) in self._buckets.items()
        ]
        for frac, key in levels:
            if frac >= 1.0:
                del self._buckets[key]
        if len(self._buckets) < self.max_buckets:
            return
        target = max(1, int(self.max_buckets * _EVICT_FRACTION))
        remaining = sorted((f, k) for f, k in levels if k in self._buckets)
        for _, key in remaining[-target:]:
            self._buckets.pop(key, None)

    def allow(self, key: str, capacity: int | None = None) -> bool:
        cap = self._cap(capacity)
        now = time.time()
        if len(self._buckets) >= self.max_buckets:
            self._reclaim(now)
        tokens, _ = self._tokens_now(key, now, cap)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now, cap)
            return False
        self._buckets[key] = (tokens - 1.0, now, cap)
        return True

    def remaining(self, key: str, capacity: int | None = None) -> int:
        """Whole tokens currently available to ``key`` — a non-consuming peek."""
        tokens, _ = self._tokens_now(key, time.time(), capacity)
        return int(tokens)

    def retry_after(self, key: str, capacity: int | None = None) -> int:
        """Whole seconds until ``key`` has a token again (>= 1; 0 if available now).

        A peek — it does not consume a token — so it is safe to call right after
        ``allow`` returns ``False`` to populate a ``Retry-After`` header.
        """
        cap = self._cap(capacity)
        now = time.time()
        tokens, _ = self._tokens_now(key, now, cap)
        if tokens >= 1.0:
            return 0
        seconds = (1.0 - tokens) * self.window / cap
        return max(1, math.ceil(seconds))


class AsyncRateLimiter(Protocol):
    """What the request middleware needs from a rate limiter.

    ``capacity`` overrides the constructor capacity for that key only —
    per-client overrides (``REKAI_CLIENT_RATE_LIMITS``) ride on it.
    """

    async def allow(self, key: str, capacity: int | None = None) -> bool: ...
    async def remaining(self, key: str, capacity: int | None = None) -> int: ...
    async def retry_after(self, key: str, capacity: int | None = None) -> int: ...
    @property
    def label(self) -> str: ...


class LocalRateLimiter:
    """Async facade over the in-process token bucket."""

    def __init__(self, capacity: int, window: float) -> None:
        self._limiter = RateLimiter(capacity, window)

    async def allow(self, key: str, capacity: int | None = None) -> bool:
        return self._limiter.allow(key, capacity)

    async def remaining(self, key: str, capacity: int | None = None) -> int:
        return self._limiter.remaining(key, capacity)

    async def retry_after(self, key: str, capacity: int | None = None) -> int:
        return self._limiter.retry_after(key, capacity)

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

    async def allow(self, key: str, capacity: int | None = None) -> bool:
        cap = self.capacity if capacity is None else capacity
        now = time.time()
        try:
            count = await self._client.incr(self._window_key(key, now))
            if count == 1:
                # Keep the counter one window past its end so a straggling
                # remaining()/retry_after() peek still sees it.
                await self._client.expire(self._window_key(key, now), int(self.window * 2))
            return int(count) <= cap
        except Exception as exc:
            logger.warning("rate limiter failing open (redis error: %s)", exc)
            return True

    async def remaining(self, key: str, capacity: int | None = None) -> int:
        cap = self.capacity if capacity is None else capacity
        now = time.time()
        try:
            raw = await self._client.get(self._window_key(key, now))
        except Exception as exc:
            logger.warning("rate limiter failing open (redis error: %s)", exc)
            return cap
        used = int(raw) if raw else 0
        return max(0, cap - used)

    async def retry_after(self, key: str, capacity: int | None = None) -> int:
        # A fixed window admits new requests only when the window rolls over.
        if await self.remaining(key, capacity) > 0:
            return 0
        return self._seconds_left_in_window(time.time())

    @property
    def label(self) -> str:
        return "redis"


def build_rate_limiter(settings: Settings, capacity: int, window: float) -> AsyncRateLimiter:
    """Redis-shared when ``REKAI_REDIS_URL`` is set, else process-local.

    ``capacity``/``window`` are passed explicitly (rather than read directly
    off ``settings.rate_limit_*``) so the same factory builds both the tenant
    limiter and a separately-sized one for ``/admin/*``."""
    if settings.redis_url:
        try:
            return RedisRateLimiter(settings.redis_url, capacity, window)
        except Exception:  # pragma: no cover - fall back if redis client init fails
            pass
    return LocalRateLimiter(capacity, window)
