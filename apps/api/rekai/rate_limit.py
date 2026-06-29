"""A simple in-memory token-bucket rate limiter, keyed per client."""

from __future__ import annotations

import math
import time


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
