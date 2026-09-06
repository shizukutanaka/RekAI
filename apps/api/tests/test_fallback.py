"""Tests for provider fallback/failover."""

from __future__ import annotations

import pytest

from rekai.cache import MemoryCache, NullCache
from rekai.config import Settings
from rekai.providers import register_provider
from rekai.providers.base import Provider, ProviderError, ProviderResult
from rekai.schemas import ChatMessage, ChatRequest
from rekai.service import handle_chat


class FlakyProvider(Provider):
    """Always fails with the configured status code."""

    requires_key = False

    def __init__(self, name: str, status_code: int) -> None:
        self.name = name
        self._status = status_code

    async def chat(self, request, api_key) -> ProviderResult:
        raise ProviderError(f"{self.name} boom", status_code=self._status)


@pytest.fixture(autouse=True)
def _register_flaky():
    register_provider(FlakyProvider("flaky5xx", 503))
    register_provider(FlakyProvider("flaky4xx", 400))


def _req(**kwargs) -> ChatRequest:
    kwargs.setdefault("messages", [ChatMessage(role="user", content="hi")])
    return ChatRequest(**kwargs)


def _settings(**kwargs) -> Settings:
    kwargs.setdefault("default_provider", "echo")
    # These tests exercise fallback, not retry — keep a single try per target.
    kwargs.setdefault("retry_max_attempts", 1)
    return Settings(**kwargs)


async def test_request_fallback_on_5xx() -> None:
    request = _req(
        model="x",
        provider="flaky5xx",
        fallbacks=[{"provider": "echo", "model": "echo"}],
    )
    resp = await handle_chat(request, None, _settings(), NullCache())
    assert resp.provider == "echo"
    assert resp.fallback_used is True
    assert resp.content.startswith("Echo:")


async def test_no_fallback_on_4xx() -> None:
    request = _req(
        model="x",
        provider="flaky4xx",
        fallbacks=[{"provider": "echo"}],
    )
    # 4xx is a client error -> should NOT fall through; the error propagates.
    with pytest.raises(ProviderError) as exc:
        await handle_chat(request, None, _settings(), NullCache())
    assert exc.value.status_code == 400


async def test_server_default_chain_used_when_enabled() -> None:
    request = _req(model="x", provider="flaky5xx")  # no per-request fallbacks
    settings = _settings(fallback_enabled=True, fallback_targets="echo")
    resp = await handle_chat(request, None, settings, NullCache())
    assert resp.provider == "echo"
    assert resp.fallback_used is True


async def test_no_fallback_when_disabled() -> None:
    request = _req(model="x", provider="flaky5xx")
    settings = _settings(fallback_enabled=False, fallback_targets="echo")
    with pytest.raises(ProviderError):
        await handle_chat(request, None, settings, NullCache())


async def test_all_fallbacks_fail_raises_last() -> None:
    request = _req(
        model="x",
        provider="flaky5xx",
        fallbacks=[{"provider": "flaky5xx"}, {"provider": "flaky4xx"}],
    )
    # flaky5xx (503) -> flaky5xx skipped as duplicate? no, different attempt;
    # chain: flaky5xx(503) -> flaky4xx(400) which is terminal.
    with pytest.raises(ProviderError) as exc:
        await handle_chat(request, None, _settings(), NullCache())
    assert exc.value.status_code == 400


async def test_primary_success_no_fallback() -> None:
    request = _req(model="echo", fallbacks=[{"provider": "flaky5xx"}])
    resp = await handle_chat(request, None, _settings(), NullCache())
    assert resp.provider == "echo"
    assert resp.fallback_used is False


class RecoveringProvider(Provider):
    """Fails with a 5xx the first call, then succeeds — simulating a transient blip."""

    name = "recovering"
    requires_key = False

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, request, api_key) -> ProviderResult:
        self.calls += 1
        if self.calls == 1:
            raise ProviderError("transient", status_code=503)
        return ProviderResult(content="recovered", model=request.model)


async def test_retry_recovers_without_fallback(monkeypatch) -> None:
    import rekai.retry

    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(rekai.retry.asyncio, "sleep", _no_sleep)
    provider = RecoveringProvider()
    register_provider(provider)

    request = _req(model="m", provider="recovering")
    resp = await handle_chat(request, None, _settings(retry_max_attempts=2), NullCache())
    assert resp.content == "recovered"
    assert resp.fallback_used is False  # recovered on the same target, no fallback
    assert provider.calls == 2  # one failure + one successful retry


class Always429(Provider):
    """Always rate-limited, with an upstream Retry-After."""

    name = "always429"
    requires_key = False

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, request, api_key) -> ProviderResult:
        self.calls += 1
        raise ProviderError("rate limited", status_code=429, retry_after=30)


async def test_cooldown_skips_rate_limited_provider() -> None:
    from rekai.cooldown import cooldowns

    cooldowns.clear()
    provider = Always429()
    register_provider(provider)
    request = _req(
        model="x", provider="always429", fallbacks=[{"provider": "echo", "model": "echo"}]
    )
    settings = _settings(retry_max_attempts=1)  # no in-place retry; cooldown on by default

    # First request: always429 429s -> fails over to echo, and is parked.
    r1 = await handle_chat(request, None, settings, NullCache())
    assert r1.provider == "echo" and r1.fallback_used is True
    assert provider.calls == 1

    # Second request: always429 is cooling down -> skipped entirely; echo serves.
    r2 = await handle_chat(request, None, settings, NullCache())
    assert r2.provider == "echo"
    assert provider.calls == 1  # not called again while cooling down
    cooldowns.clear()


async def test_cooldown_shared_via_cache_across_workers() -> None:
    """A cooldown recorded while handling one request is visible to a request
    handled by a "different worker" (an empty local dict) that shares the same
    cache backend — the scenario a Redis-backed cache is meant to fix."""
    from rekai.cooldown import cooldowns

    cooldowns.clear()
    shared_cache = MemoryCache()  # stands in for a shared Redis instance
    provider = Always429()
    register_provider(provider)
    request = _req(
        model="x", provider="always429", fallbacks=[{"provider": "echo", "model": "echo"}]
    )
    settings = _settings(retry_max_attempts=1)

    r1 = await handle_chat(request, None, settings, shared_cache)
    assert r1.provider == "echo"
    assert provider.calls == 1

    # Simulate a second worker process: its local cooldown dict never saw the
    # 429 from the first request, but it shares the same cache backend.
    cooldowns._until.clear()
    r2 = await handle_chat(request, None, settings, shared_cache)
    assert r2.provider == "echo"
    assert provider.calls == 1  # skipped via the shared cache, not called again
    cooldowns.clear()


class Always5xx(Provider):
    """Always fails with a 5xx (no explicit Retry-After, unlike a 429)."""

    name = "always5xx"
    requires_key = False

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, request, api_key) -> ProviderResult:
        self.calls += 1
        raise ProviderError("upstream down", status_code=503)


async def test_circuit_breaker_parks_after_threshold_consecutive_5xx() -> None:
    from rekai.circuit_breaker import consecutive_failures
    from rekai.cooldown import cooldowns

    cooldowns.clear()
    consecutive_failures.clear()
    provider = Always5xx()
    register_provider(provider)
    request = _req(
        model="x", provider="always5xx", fallbacks=[{"provider": "echo", "model": "echo"}]
    )
    settings = _settings(retry_max_attempts=1, circuit_breaker_threshold=3)

    # First 3 requests: always5xx fails every time (below/at threshold), falls
    # over to echo, and is actually called each time — not yet parked.
    for _ in range(3):
        resp = await handle_chat(request, None, settings, NullCache())
        assert resp.provider == "echo"
    assert provider.calls == 3

    # The 3rd failure tripped the breaker: the 4th request skips it entirely.
    resp = await handle_chat(request, None, settings, NullCache())
    assert resp.provider == "echo"
    assert provider.calls == 3  # not called again while cooling down
    cooldowns.clear()
    consecutive_failures.clear()


class FailTwiceThenRecover(Provider):
    """Fails with a 5xx on its first two calls, then succeeds from then on."""

    name = "fail_twice_recover"
    requires_key = False

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, request, api_key) -> ProviderResult:
        self.calls += 1
        if self.calls <= 2:
            raise ProviderError("transient", status_code=503)
        return ProviderResult(content="recovered", model=request.model)


async def test_circuit_breaker_resets_on_success() -> None:
    from rekai.circuit_breaker import consecutive_failures
    from rekai.cooldown import cooldowns

    cooldowns.clear()
    consecutive_failures.clear()
    provider = FailTwiceThenRecover()
    register_provider(provider)
    request = _req(model="x", provider="fail_twice_recover")
    # retry off (in-place retry would recover within one request); threshold 3
    # so 2 failures alone wouldn't trip it anyway — the point is the reset.
    settings = _settings(retry_max_attempts=1, circuit_breaker_threshold=3)

    # Two failing requests, then a successful one resets the count to zero.
    with pytest.raises(ProviderError):
        await handle_chat(request, None, settings, NullCache())
    with pytest.raises(ProviderError):
        await handle_chat(request, None, settings, NullCache())
    resp = await handle_chat(request, None, settings, NullCache())
    assert resp.content == "recovered"

    # Two more failures now (not three) should NOT trip the breaker, since the
    # success above reset the count back to zero.
    provider.calls = 0  # let it fail on the next two calls again
    with pytest.raises(ProviderError):
        await handle_chat(request, None, settings, NullCache())
    with pytest.raises(ProviderError):
        await handle_chat(request, None, settings, NullCache())
    assert consecutive_failures.record_failure("fail_twice_recover") == 3  # 2 + this probe
    cooldowns.clear()
    consecutive_failures.clear()


async def test_circuit_breaker_disabled_by_provider_cooldown_enabled_flag() -> None:
    from rekai.circuit_breaker import consecutive_failures
    from rekai.cooldown import cooldowns

    cooldowns.clear()
    consecutive_failures.clear()
    provider = Always5xx()
    register_provider(provider)
    request = _req(
        model="x", provider="always5xx", fallbacks=[{"provider": "echo", "model": "echo"}]
    )
    settings = _settings(
        retry_max_attempts=1, circuit_breaker_threshold=1, provider_cooldown_enabled=False
    )

    for _ in range(5):
        resp = await handle_chat(request, None, settings, NullCache())
        assert resp.provider == "echo"
    assert provider.calls == 5  # never parked -> called on every request
    cooldowns.clear()
    consecutive_failures.clear()


async def test_circuit_breaker_threshold_of_one_parks_immediately() -> None:
    from rekai.circuit_breaker import consecutive_failures
    from rekai.cooldown import cooldowns

    cooldowns.clear()
    consecutive_failures.clear()
    provider = Always5xx()
    register_provider(provider)
    request = _req(
        model="x", provider="always5xx", fallbacks=[{"provider": "echo", "model": "echo"}]
    )
    settings = _settings(retry_max_attempts=1, circuit_breaker_threshold=1)

    await handle_chat(request, None, settings, NullCache())
    assert provider.calls == 1
    await handle_chat(request, None, settings, NullCache())
    assert provider.calls == 1  # parked after just one failure
    cooldowns.clear()
    consecutive_failures.clear()


async def test_request_fallbacks_are_checked_against_the_allowlist() -> None:
    # `fallbacks` is client-steered, so an off-list target is a 403 — not a
    # silent skip that would leave the caller believing it has a chain it hasn't.
    request = _req(model="gpt-4o-mini", fallbacks=[{"provider": "anthropic"}])
    settings = _settings(allowed_providers="openai")
    with pytest.raises(ProviderError) as exc:
        await handle_chat(request, None, settings, NullCache())
    assert exc.value.status_code == 403


async def test_allowed_request_fallbacks_still_work() -> None:
    register_provider(FlakyProvider("flaky502b", 502))
    request = _req(model="x", provider="flaky502b", fallbacks=[{"provider": "echo"}])
    settings = _settings(allowed_providers="flaky502b,echo")
    resp = await handle_chat(request, None, settings, NullCache())
    assert resp.provider == "echo"
    assert resp.fallback_used is True


async def test_every_upstream_failure_is_counted_per_provider() -> None:
    # Recorded for all attempts, not only the ones that trigger a fallback:
    # a per-provider success rate needs the full denominator, and the fallback
    # branch alone drops the last attempt in a chain.
    from rekai.metrics import metrics

    register_provider(FlakyProvider("flaky-count", 502))
    metrics.provider_errors.clear()
    request = _req(model="x", provider="flaky-count", fallbacks=[{"provider": "echo"}])
    await handle_chat(request, None, _settings(), NullCache())

    # No fallback this time: the failure is the last attempt, and still counted.
    with pytest.raises(ProviderError):
        await handle_chat(_req(model="x", provider="flaky-count"), None, _settings(), NullCache())

    assert metrics.provider_errors[("flaky-count", 502)] == 2
    metrics.provider_errors.clear()


# --- an unknown request-level fallback is a 400, not a silent drop ------------
# `_build_attempts` runs `ensure_allowed` over request-level fallbacks because,
# in its own words, "quietly dropping it would leave the client believing it had
# a fallback chain it doesn't have". But `ensure_allowed` only checks the
# operator's allowlist. A *nonexistent* provider passed it and was then dropped
# by the `provider is None` skip further down — so the caller got the primary's
# error with no sign their chain had been ignored. A typo is much likelier than
# an allowlist violation.


async def test_unknown_request_level_fallback_is_rejected() -> None:
    request = _req(
        model="x",
        provider="flaky5xx",
        fallbacks=[{"provider": "totally-made-up", "model": "x"}],
    )

    with pytest.raises(ProviderError) as excinfo:
        await handle_chat(request, None, _settings(), NullCache())

    # Matches how an unknown *primary* provider is already reported.
    assert excinfo.value.status_code == 400
    assert "totally-made-up" in str(excinfo.value)


async def test_an_unknown_fallback_is_not_masked_by_a_working_one() -> None:
    # The dangerous shape: one good target hides the typo'd one, so the chain
    # looks like it worked and the mistake ships.
    request = _req(
        model="x",
        provider="flaky5xx",
        fallbacks=[{"provider": "opneai", "model": "x"}, {"provider": "echo", "model": "echo"}],
    )

    with pytest.raises(ProviderError) as excinfo:
        await handle_chat(request, None, _settings(), NullCache())

    assert excinfo.value.status_code == 400
    assert "opneai" in str(excinfo.value)


async def test_a_valid_request_level_fallback_still_works() -> None:
    request = _req(
        model="x", provider="flaky5xx", fallbacks=[{"provider": "echo", "model": "echo"}]
    )

    resp = await handle_chat(request, None, _settings(), NullCache())

    assert resp.provider == "echo"
    assert resp.fallback_used is True


async def test_a_server_configured_unknown_fallback_is_still_skipped() -> None:
    # Deliberately asymmetric. A bad REKAI_FALLBACK_TARGETS entry is an operator
    # misconfiguration, and failing every request over it would be worse than
    # logging and moving on — unlike a request-level target, the caller cannot
    # fix it and did not ask for it.
    settings = _settings(fallback_enabled=True, fallback_targets="totally-made-up:x,echo:echo")
    request = _req(model="x", provider="flaky5xx")

    resp = await handle_chat(request, None, settings, NullCache())

    assert resp.provider == "echo"
    assert resp.fallback_used is True
