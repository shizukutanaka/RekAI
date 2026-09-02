"""Tests for the total-request deadline (REKAI_REQUEST_DEADLINE_SECONDS).

``request_timeout_seconds`` bounds *one* upstream call. Attempts multiply it by
``retry_max_attempts`` and again by the length of the fallback chain, so before
this existed a client could wait ``targets x attempts x timeout`` — ~384s on the
shipped defaults — while holding a connection and a concurrency slot. This is
the split Envoy draws between ``route.timeout`` and ``retry_policy.
per_try_timeout``; RekAI only had the per-try half.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from rekai.cache import NullCache
from rekai.config import Settings
from rekai.main import create_app
from rekai.providers import register_provider
from rekai.providers.base import Provider, ProviderError, ProviderResult
from rekai.retry import DeadlineExceeded, call_with_retry, remaining_budget
from rekai.schemas import ChatMessage, ChatRequest
from rekai.service import handle_chat


class SlowFailingProvider(Provider):
    """Burns ``delay`` seconds, then fails transiently — a hung upstream."""

    requires_key = False

    def __init__(self, name: str, delay: float) -> None:
        self.name = name
        self._delay = delay
        self.calls = 0

    async def chat(self, request, api_key) -> ProviderResult:
        self.calls += 1
        await asyncio.sleep(self._delay)
        raise ProviderError(f"{self.name} timed out", status_code=504)


class HangingProvider(Provider):
    """Never returns within any sane budget."""

    requires_key = False
    name = "hanging"

    async def chat(self, request, api_key) -> ProviderResult:
        await asyncio.sleep(30)
        raise AssertionError("should have been cut off by the deadline")


# --- remaining_budget ---------------------------------------------------------


def test_remaining_budget_is_none_when_unbounded() -> None:
    assert remaining_budget(None) is None


def test_remaining_budget_never_goes_negative() -> None:
    spent = remaining_budget(time.monotonic() - 5)
    assert spent == 0.0

    left = remaining_budget(time.monotonic() + 5)
    assert left is not None and 4.0 < left <= 5.0


# --- call_with_retry ----------------------------------------------------------


async def test_no_deadline_keeps_todays_behavior() -> None:
    # deadline=None (the default, and what request_deadline_seconds=0 produces)
    # must not change the attempt count or the outcome.
    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ProviderError("blip", status_code=503)
        return "ok"

    async def no_sleep(_: float) -> None:
        return None

    got = await call_with_retry(
        fn, attempts=3, base_delay=0.5, max_delay=8.0, sleep=no_sleep, rand=lambda: 1.0
    )
    assert got == "ok"
    assert calls["n"] == 3


async def test_expired_deadline_refuses_to_start_an_attempt() -> None:
    async def fn() -> str:
        raise AssertionError("must not be called")

    with pytest.raises(DeadlineExceeded):
        await call_with_retry(
            fn, attempts=3, base_delay=0.5, max_delay=8.0, deadline=time.monotonic() - 1
        )


async def test_a_hung_attempt_is_cut_off_at_the_deadline() -> None:
    # The point of the change: one attempt that never returns used to be bounded
    # only by httpx's own timeout, so the *request* had no bound at all.
    async def fn() -> str:
        await asyncio.sleep(30)
        return "never"

    started = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        await call_with_retry(
            fn, attempts=3, base_delay=0.5, max_delay=8.0, deadline=time.monotonic() + 0.05
        )
    assert time.monotonic() - started < 5.0


async def test_deadline_exceeded_is_not_retried() -> None:
    # It is a 504 and 5xx is normally retryable, but the budget is what ran out
    # — another attempt cannot succeed, it can only make the client wait longer.
    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        await asyncio.sleep(30)
        return "never"

    with pytest.raises(DeadlineExceeded):
        await call_with_retry(
            fn, attempts=5, base_delay=0.0, max_delay=0.0, deadline=time.monotonic() + 0.05
        )
    assert calls["n"] == 1


async def test_backoff_never_sleeps_past_the_deadline() -> None:
    # A retry that can't happen isn't worth waiting for: raise the real upstream
    # error now rather than burning the remaining budget in sleep() first.
    slept: list[float] = []

    async def fn() -> str:
        raise ProviderError("upstream blip", status_code=503)

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    with pytest.raises(ProviderError) as excinfo:
        await call_with_retry(
            fn,
            attempts=5,
            base_delay=8.0,
            max_delay=8.0,
            sleep=fake_sleep,
            rand=lambda: 1.0,
            deadline=time.monotonic() + 0.5,  # < the 8s backoff
        )
    assert not isinstance(excinfo.value, DeadlineExceeded)
    assert excinfo.value.status_code == 503  # the real cause, not the budget
    assert slept == []


async def test_backoff_still_sleeps_when_it_fits() -> None:
    slept: list[float] = []
    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise ProviderError("blip", status_code=503)
        return "ok"

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    got = await call_with_retry(
        fn,
        attempts=3,
        base_delay=0.01,
        max_delay=0.01,
        sleep=fake_sleep,
        rand=lambda: 1.0,
        deadline=time.monotonic() + 30,
    )
    assert got == "ok"
    assert slept == [0.01]


# --- the fallback chain -------------------------------------------------------


def _req(**kwargs) -> ChatRequest:
    kwargs.setdefault("messages", [ChatMessage(role="user", content="hi")])
    return ChatRequest(**kwargs)


async def test_chain_stops_starting_targets_once_the_budget_is_spent() -> None:
    slow = SlowFailingProvider("slow_chain", 0.15)
    never = SlowFailingProvider("never_reached", 0.15)
    register_provider(slow)
    register_provider(never)

    request = _req(
        model="x",
        provider="slow_chain",
        fallbacks=[
            {"provider": "never_reached", "model": "x"},
            {"provider": "never_reached", "model": "y"},
        ],
    )
    settings = Settings(
        environment="test",
        default_provider="echo",
        retry_max_attempts=1,
        request_deadline_seconds=0.1,
    )

    started = time.monotonic()
    with pytest.raises(ProviderError):
        await handle_chat(request, None, settings, NullCache())
    elapsed = time.monotonic() - started

    # Without a deadline this is 3 x 0.15s. With one, the chain stops after the
    # budget is spent instead of walking every remaining target.
    assert elapsed < 0.3
    assert never.calls == 0


async def test_unlimited_deadline_walks_the_whole_chain() -> None:
    # The default (0) must keep failing over, however long the chain is.
    slow = SlowFailingProvider("slow_unbounded", 0.01)
    register_provider(slow)

    request = _req(
        model="x",
        provider="slow_unbounded",
        fallbacks=[{"provider": "echo", "model": "echo"}],
    )
    settings = Settings(environment="test", default_provider="echo", retry_max_attempts=1)
    assert settings.request_deadline_seconds == 0.0

    resp = await handle_chat(request, None, settings, NullCache())
    assert resp.provider == "echo"
    assert resp.fallback_used is True


# --- the endpoint -------------------------------------------------------------


def test_endpoint_returns_504_when_the_budget_runs_out() -> None:
    register_provider(HangingProvider())
    settings = Settings(
        environment="test",
        default_provider="echo",
        rate_limit_enabled=False,
        request_deadline_seconds=0.1,
    )
    client = TestClient(create_app(settings))

    started = time.monotonic()
    resp = client.post(
        "/v1/chat",
        json={"model": "x", "provider": "hanging", "messages": [{"role": "user", "content": "hi"}]},
    )
    elapsed = time.monotonic() - started

    assert resp.status_code == 504
    assert resp.json()["error"] == "provider_error"
    # Released at the budget, not after retry_max_attempts x request_timeout.
    assert elapsed < 5.0
