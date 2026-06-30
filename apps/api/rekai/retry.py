"""Bounded automatic retry with exponential backoff + full jitter.

Transient upstream failures (5xx / network timeouts surfaced as a 5xx
``ProviderError``) are retried a few times before RekAI gives up or falls over
to the next provider. Client errors (4xx) are never retried. Backoff uses full
jitter — ``uniform(0, min(max, base * 2**attempt))`` — so concurrent clients
don't synchronise their retries and hammer a recovering upstream.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from rekai.providers.base import ProviderError

T = TypeVar("T")


def backoff_delay(
    attempt: int,
    base_delay: float,
    max_delay: float,
    rand: Callable[[], float] = random.random,
) -> float:
    """Full-jitter backoff for a zero-based ``attempt`` index (>= 0)."""
    ceiling = min(max_delay, base_delay * (2**attempt))
    return rand() * ceiling


async def call_with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay: float,
    max_delay: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float] = random.random,
) -> T:
    """Call ``fn`` up to ``attempts`` times, retrying only transient (5xx) errors.

    ``attempts`` is the total number of tries (1 disables retrying). A 4xx
    ``ProviderError`` is re-raised immediately; the last error is raised once the
    budget is exhausted.
    """
    total = max(1, attempts)
    last: ProviderError | None = None
    for i in range(total):
        try:
            return await fn()
        except ProviderError as exc:
            last = exc
            if i + 1 >= total:
                raise  # out of attempts
            delay = _retry_delay(exc, i, base_delay, max_delay, rand)
            if delay is None:
                raise  # not retryable (4xx, or a 429 asking us to wait too long)
            await sleep(delay)
    assert last is not None  # unreachable: the loop body always returns or raises
    raise last


def _retry_delay(
    exc: ProviderError, attempt: int, base_delay: float, max_delay: float, rand: Callable[[], float]
) -> float | None:
    """How long to wait before retrying ``exc``, or ``None`` if it shouldn't be.

    - 5xx (incl. timeouts) → jittered exponential backoff.
    - 429 → honour the upstream ``Retry-After`` when present and not longer than
      ``max_delay`` (otherwise surface it so the client backs off); fall back to
      backoff when no header was sent.
    - 4xx → never retried.
    """
    if exc.status_code == 429:
        if exc.retry_after is None:
            return backoff_delay(attempt, base_delay, max_delay, rand)
        return exc.retry_after if exc.retry_after <= max_delay else None
    if exc.status_code >= 500:
        return backoff_delay(attempt, base_delay, max_delay, rand)
    return None
