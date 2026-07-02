"""Consecutive-failure counting that extends provider cooldown to repeated 5xx.

Cooldown (``rekai/cooldown.py``) previously only parked a provider on a 429 —
an explicit "back off" signal. A provider returning 500s got no such break:
every request paid for a full retry-with-backoff cycle before falling over to
a fallback, request after request. A single 5xx surfacing after retries
already represents several failed HTTP calls (see ``retry.py``), so this
additionally counts *across separate requests*: only after ``threshold``
requests in a row fail for a provider does it get parked via the existing
cooldown mechanism — avoiding overreacting to one bad request. Any success
resets the count. Process-local by design (unlike cooldown, this doesn't need
cross-worker sharing — each worker converges on its own within a few requests).
"""

from __future__ import annotations

import threading


class ConsecutiveFailureTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def record_failure(self, key: str) -> int:
        """Increment ``key``'s consecutive-failure count; return the new count."""
        with self._lock:
            count = self._counts.get(key, 0) + 1
            self._counts[key] = count
            return count

    def record_success(self, key: str) -> None:
        with self._lock:
            self._counts.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._counts.clear()


# Module-level singleton (mirrors rekai.metrics.metrics / rekai.cooldown.cooldowns).
consecutive_failures = ConsecutiveFailureTracker()
