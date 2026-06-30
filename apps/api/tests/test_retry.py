"""Tests for bounded retry with exponential backoff + jitter."""

from __future__ import annotations

import pytest

from rekai.providers.base import ProviderError
from rekai.retry import backoff_delay, call_with_retry


def test_backoff_grows_and_is_capped() -> None:
    # rand=lambda: 1.0 removes jitter so we see the ceiling.
    full = lambda: 1.0  # noqa: E731
    assert backoff_delay(0, 0.5, 8.0, full) == 0.5
    assert backoff_delay(1, 0.5, 8.0, full) == 1.0
    assert backoff_delay(2, 0.5, 8.0, full) == 2.0
    # Capped at max_delay.
    assert backoff_delay(10, 0.5, 8.0, full) == 8.0


def test_backoff_applies_jitter() -> None:
    # Full jitter: uniform(0, ceiling); rand=0.5 -> half the ceiling.
    assert backoff_delay(1, 0.5, 8.0, lambda: 0.5) == 0.5


async def test_retries_transient_then_succeeds() -> None:
    calls = {"n": 0}
    slept: list[float] = []

    async def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ProviderError("upstream blip", status_code=503)
        return "ok"

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    result = await call_with_retry(
        fn, attempts=3, base_delay=0.5, max_delay=8.0, sleep=fake_sleep, rand=lambda: 1.0
    )
    assert result == "ok"
    assert calls["n"] == 3
    assert slept == [0.5, 1.0]  # backed off between the two retries


async def test_does_not_retry_client_errors() -> None:
    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        raise ProviderError("bad request", status_code=400)

    with pytest.raises(ProviderError) as exc:
        await call_with_retry(fn, attempts=3, base_delay=0.1, max_delay=1.0)
    assert exc.value.status_code == 400
    assert calls["n"] == 1  # 4xx is terminal


async def test_raises_after_exhausting_attempts() -> None:
    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        raise ProviderError("still down", status_code=502)

    async def fake_sleep(d: float) -> None:
        return None

    with pytest.raises(ProviderError):
        await call_with_retry(fn, attempts=2, base_delay=0.1, max_delay=1.0, sleep=fake_sleep)
    assert calls["n"] == 2


async def test_retries_429_honoring_retry_after() -> None:
    calls = {"n": 0}
    slept: list[float] = []

    async def fn() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderError("slow down", status_code=429, retry_after=3.0)
        return "ok"

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    result = await call_with_retry(fn, attempts=2, base_delay=0.5, max_delay=8.0, sleep=fake_sleep)
    assert result == "ok"
    assert slept == [3.0]  # waited exactly the upstream Retry-After, not backoff


async def test_429_with_long_retry_after_is_surfaced() -> None:
    # If the upstream asks us to wait longer than max_delay, don't block — let
    # the client handle it (it gets the Retry-After).
    async def fn() -> str:
        raise ProviderError("slow down", status_code=429, retry_after=120.0)

    with pytest.raises(ProviderError) as exc:
        await call_with_retry(fn, attempts=3, base_delay=0.5, max_delay=8.0)
    assert exc.value.retry_after == 120.0


async def test_429_without_header_uses_backoff() -> None:
    calls = {"n": 0}
    slept: list[float] = []

    async def fn() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderError("slow down", status_code=429)  # no Retry-After
        return "ok"

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    await call_with_retry(
        fn, attempts=2, base_delay=0.5, max_delay=8.0, sleep=fake_sleep, rand=lambda: 1.0
    )
    assert slept == [0.5]  # fell back to backoff


async def test_attempts_one_disables_retry() -> None:
    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        raise ProviderError("down", status_code=500)

    with pytest.raises(ProviderError):
        await call_with_retry(fn, attempts=1, base_delay=0.1, max_delay=1.0)
    assert calls["n"] == 1
