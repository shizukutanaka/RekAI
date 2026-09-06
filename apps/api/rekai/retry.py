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
import time
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


def remaining_budget(deadline: float | None) -> float | None:
    """Seconds left before ``deadline`` (a ``time.monotonic()`` stamp), or None
    when unbounded. Never negative — 0.0 means "already spent"."""
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


class DeadlineExceeded(ProviderError):
    """The request's total budget ran out (REKAI_REQUEST_DEADLINE_SECONDS).

    A 504: the gateway itself gave up waiting, which is distinct from an
    upstream returning one.
    """

    def __init__(self, message: str = "Request deadline exceeded.") -> None:
        super().__init__(message, status_code=504)


async def call_with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay: float,
    max_delay: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float] = random.random,
    on_retry: Callable[[], None] | None = None,
    deadline: float | None = None,
) -> T:
    """Call ``fn`` up to ``attempts`` times, retrying only transient (5xx) errors.

    ``attempts`` is the total number of tries (1 disables retrying). A 4xx
    ``ProviderError`` is re-raised immediately; the last error is raised once the
    budget is exhausted.

    ``deadline`` is a ``time.monotonic()`` stamp bounding the whole call —
    including the individual attempt, which is wrapped so one hung upstream
    can't overrun the budget on its own, and the backoff sleep, which is never
    allowed to sleep past it. Without it, ``attempts`` multiplies the per-call
    timeout with nothing bounding the product.
    """
    total = max(1, attempts)
    last: ProviderError | None = None
    for i in range(total):
        left = remaining_budget(deadline)
        if left is not None and left <= 0:
            raise last or DeadlineExceeded()
        try:
            if left is None:
                return await fn()
            try:
                return await asyncio.wait_for(fn(), timeout=left)
            except (TimeoutError, asyncio.TimeoutError) as exc:
                raise DeadlineExceeded() from exc
        except ProviderError as exc:
            last = exc
            if isinstance(exc, DeadlineExceeded):
                raise  # the budget is gone; another attempt can't help
            if i + 1 >= total:
                raise  # out of attempts
            delay = _retry_delay(exc, i, base_delay, max_delay, rand)
            if delay is None:
                raise  # not retryable (4xx, or a 429 asking us to wait too long)
            left = remaining_budget(deadline)
            if left is not None and delay >= left:
                # Sleeping would consume what's left and still leave no time to
                # actually retry, so fail now with the real upstream error
                # rather than burning the remainder first.
                raise
            if on_retry is not None:
                on_retry()
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
