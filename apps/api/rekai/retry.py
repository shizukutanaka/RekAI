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
    last: ProviderError | None = None
    for i in range(max(1, attempts)):
        try:
            return await fn()
        except ProviderError as exc:
            last = exc
            if exc.status_code < 500 or i + 1 >= max(1, attempts):
                raise
            await sleep(backoff_delay(i, base_delay, max_delay, rand))
    assert last is not None  # unreachable: the loop body always returns or raises
    raise last
