"""A simple in-memory token-bucket rate limiter, keyed per client."""

from __future__ import annotations

import time


class RateLimiter:
    """Fixed-window token bucket.

    Each client gets ``capacity`` tokens that refill over ``window`` seconds.
    Suitable for single-process deployments; use a shared store for multi-node.
    """

    def __init__(self, capacity: int, window: float) -> None:
        self.capacity = capacity
        self.window = window
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_refill)

    def allow(self, key: str) -> bool:
        now = time.time()
        tokens, last = self._buckets.get(key, (float(self.capacity), now))
        # Refill proportional to elapsed time.
        refill = (now - last) * (self.capacity / self.window)
        tokens = min(self.capacity, tokens + refill)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        return True
