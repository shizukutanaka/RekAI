import logging
import time
import types

import pytest
from fastapi.testclient import TestClient

import rekai.main as main_module
from rekai.auth import client_id
from rekai.config import Settings
from rekai.main import create_app
from rekai.providers import register_provider
from rekai.providers.base import Provider, ProviderError
from rekai.rate_limit import RateLimiter, RedisRateLimiter, build_rate_limiter
from rekai.security import KeyCipher, generate_key, mask_key


def test_upstream_429_retry_after_propagated_to_client() -> None:
    class RateLimitedProvider(Provider):
        name = "upstream_rl"
        requires_key = False

        async def chat(self, request, api_key):
            raise ProviderError("upstream rate limit", status_code=429, retry_after=15)

    register_provider(RateLimitedProvider())
    # retry off (surface immediately) and rate limiting off (isolate the path).
    settings = Settings(
        environment="test",
        default_provider="echo",
        retry_max_attempts=1,
        rate_limit_enabled=False,
    )
    client = TestClient(create_app(settings))
    body = {
        "model": "x",
        "provider": "upstream_rl",
        "messages": [{"role": "user", "content": "hi"}],
    }
    resp = client.post("/v1/chat", json=body)
    assert resp.status_code == 429
    # The upstream's Retry-After is passed through so the client can back off.
    assert resp.headers["Retry-After"] == "15"


def test_cipher_roundtrip() -> None:
    cipher = KeyCipher(generate_key())
    token = cipher.encrypt("sk-secret")
    assert token != "sk-secret"
    assert cipher.decrypt(token) == "sk-secret"


def test_cipher_wrong_key_fails() -> None:
    token = KeyCipher(generate_key()).encrypt("x")
    with pytest.raises(ValueError):
        KeyCipher(generate_key()).decrypt(token)


@pytest.mark.parametrize(
    "key,expected",
    [(None, "<none>"), ("short", "*****"), ("sk-1234567890", "sk-1…7890")],
)
def test_mask_key(key, expected) -> None:
    assert mask_key(key) == expected


def test_rate_limiter_blocks_after_capacity() -> None:
    limiter = RateLimiter(capacity=2, window=60)
    assert limiter.allow("client") is True
    assert limiter.allow("client") is True
    assert limiter.allow("client") is False
    # A different client has its own bucket.
    assert limiter.allow("other") is True


def test_rate_limiter_reclaims_idle_buckets_before_active_ones() -> None:
    # A fully-refilled bucket is indistinguishable from a brand-new one, so it
    # carries no state and is reclaimed first; a partially-spent bucket is
    # holding a real client's consumed budget and is kept.
    limiter = RateLimiter(capacity=5, window=60, max_buckets=10)
    now = time.time()
    limiter._buckets["idle"] = (5.0, now, 5)  # tokens == capacity -> idle
    limiter._buckets["busy"] = (0.5, now, 5)  # partially spent -> active
    limiter._reclaim(now)
    assert "idle" not in limiter._buckets
    assert "busy" in limiter._buckets


def test_rate_limiter_enforces_its_bucket_cap() -> None:
    # This used to assert the opposite — that active buckets accumulate past
    # max_buckets — which encoded the unbounded growth as intended behavior.
    limiter = RateLimiter(capacity=5, window=60, max_buckets=10)
    for i in range(11):
        limiter.allow(f"active-{i}")  # each spends a token -> none is prunable
    assert len(limiter._buckets) <= 10


def test_rate_limiter_retry_after() -> None:
    limiter = RateLimiter(capacity=2, window=60)
    # Tokens available -> no wait.
    assert limiter.retry_after("client") == 0
    limiter.allow("client")
    limiter.allow("client")
    assert limiter.allow("client") is False
    # One token refills every window/capacity = 30s; peek doesn't consume.
    wait = limiter.retry_after("client")
    assert 1 <= wait <= 30
    assert limiter.retry_after("client") == wait


def test_rate_limiter_remaining() -> None:
    limiter = RateLimiter(capacity=3, window=60)
    assert limiter.remaining("client") == 3  # full, non-consuming
    assert limiter.remaining("client") == 3  # still full (peek)
    limiter.allow("client")
    assert limiter.remaining("client") == 2


class _FakeRedis:
    """Just enough of redis.asyncio for the rate limiter: INCR/EXPIRE/GET."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.fail = False

    async def incr(self, key: str) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> None:
        if self.fail:
            raise ConnectionError("redis down")
        self.ttls[key] = seconds

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise ConnectionError("redis down")
        return str(self.counts[key]) if key in self.counts else None


async def test_redis_rate_limiter_blocks_after_capacity() -> None:
    fake = _FakeRedis()
    limiter = RedisRateLimiter("redis://unused", capacity=2, window=60, client=fake)
    assert await limiter.allow("client") is True
    assert await limiter.allow("client") is True
    assert await limiter.allow("client") is False
    # A different client has its own counter.
    assert await limiter.allow("other") is True
    # The window counter got a TTL so it can't accumulate forever.
    assert all(ttl > 0 for ttl in fake.ttls.values())


async def test_redis_rate_limiter_counts_are_shared_via_the_store() -> None:
    # Two limiter instances (≈ two workers) over one Redis see one budget.
    fake = _FakeRedis()
    worker_a = RedisRateLimiter("redis://unused", capacity=2, window=60, client=fake)
    worker_b = RedisRateLimiter("redis://unused", capacity=2, window=60, client=fake)
    assert await worker_a.allow("client") is True
    assert await worker_b.allow("client") is True
    assert await worker_a.allow("client") is False
    assert await worker_b.allow("client") is False


async def test_redis_rate_limiter_remaining_and_retry_after() -> None:
    fake = _FakeRedis()
    limiter = RedisRateLimiter("redis://unused", capacity=2, window=60, client=fake)
    assert await limiter.remaining("client") == 2
    assert await limiter.retry_after("client") == 0
    await limiter.allow("client")
    await limiter.allow("client")
    assert await limiter.remaining("client") == 0
    # Blocked until the fixed window rolls over.
    assert 1 <= await limiter.retry_after("client") <= 60


async def test_redis_rate_limiter_fails_open_when_redis_is_down() -> None:
    fake = _FakeRedis()
    limiter = RedisRateLimiter("redis://unused", capacity=1, window=60, client=fake)
    fake.fail = True
    # Redis outage degrades to "no rate limiting", not "no service".
    assert await limiter.allow("client") is True
    assert await limiter.allow("client") is True
    assert await limiter.remaining("client") == 1
    assert await limiter.retry_after("client") == 0


def test_build_rate_limiter_is_local_without_redis() -> None:
    settings = Settings(environment="test", rate_limit_enabled=True)
    assert build_rate_limiter(settings, 60, 60).label == "local"


def test_endpoint_sets_ratelimit_headers() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        rate_limit_enabled=True,
        rate_limit_requests=5,
        rate_limit_window_seconds=60,
    )
    client = TestClient(create_app(settings))
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
    resp = client.post("/v1/chat", json=body)
    assert resp.status_code == 200
    assert resp.headers["X-RateLimit-Limit"] == "5"
    # One token consumed by this request -> 4 remain.
    assert resp.headers["X-RateLimit-Remaining"] == "4"


def test_options_preflight_not_rate_limited() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        rate_limit_enabled=True,
        rate_limit_requests=1,
        rate_limit_window_seconds=60,
    )
    client = TestClient(create_app(settings))
    # Consume the only token, then a CORS preflight must still pass (not 429).
    assert client.post("/v1/chat", json={"model": "echo", "messages": []}).status_code != 429
    pre = client.options(
        "/v1/chat",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert pre.status_code != 429


def test_endpoint_429_sets_retry_after() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        rate_limit_enabled=True,
        rate_limit_requests=1,
        rate_limit_window_seconds=60,
    )
    client = TestClient(create_app(settings))
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
    assert client.post("/v1/chat", json=body).status_code == 200
    blocked = client.post("/v1/chat", json=body, headers={"Origin": "http://localhost:3000"})
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert blocked.json()["error"] == "rate_limited"
    # CORS is outermost, so even this short-circuit 429 is browser-readable.
    assert blocked.headers["access-control-allow-origin"] == "*"
    # Retry-After is exposed to browser JS (not CORS-safelisted by default).
    assert "retry-after" in blocked.headers["access-control-expose-headers"].lower()


def test_client_budget_exceeded_returns_402() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        api_keys="sk-budget-a",
        rate_limit_enabled=False,
        client_budget_usd=0.5,
    )
    client = TestClient(create_app(settings))
    try:
        # Simulate prior spend past the cap (echo itself is free, so seed it directly).
        main_module.metrics.record_client_usage(client_id("sk-budget-a"), tokens=100, cost_usd=1.0)
        body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
        resp = client.post(
            "/v1/chat",
            json=body,
            headers={
                "Authorization": "Bearer sk-budget-a",
                "Origin": "http://localhost:3000",
            },
        )
        assert resp.status_code == 402
        assert resp.json()["error"] == "budget_exceeded"
        assert resp.headers["X-Budget-Remaining"] == "0"
        # CORS is outermost, so this short-circuit 402 is browser-readable too.
        assert "x-budget-remaining" in resp.headers["access-control-expose-headers"].lower()
    finally:
        main_module.metrics.seed({})


def test_client_budget_allows_requests_under_the_cap() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        api_keys="sk-budget-b",
        rate_limit_enabled=False,
        client_budget_usd=10.0,
    )
    client = TestClient(create_app(settings))
    try:
        body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
        resp = client.post("/v1/chat", json=body, headers={"Authorization": "Bearer sk-budget-b"})
        assert resp.status_code == 200
    finally:
        main_module.metrics.seed({})


def test_client_budget_window_seconds_enforces_cap_within_window(monkeypatch) -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        api_keys="sk-budget-w",
        rate_limit_enabled=False,
        client_budget_usd=0.5,
        client_budget_window_seconds=100,
    )
    client = TestClient(create_app(settings))
    try:
        monkeypatch.setattr(main_module.time, "time", lambda: 1000.0)
        # Prior spend recorded in the same window the check will read.
        main_module.metrics.record_client_budget_usage(
            client_id("sk-budget-w"), 1.0, window_seconds=100, now=1000.0
        )
        body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
        resp = client.post("/v1/chat", json=body, headers={"Authorization": "Bearer sk-budget-w"})
        assert resp.status_code == 402
        # Window 10 spans [1000, 1100) -> resets at 1100.
        assert resp.headers["X-Budget-Reset"] == "1100"
    finally:
        main_module.metrics.seed({})


def test_client_token_limit_exceeded_returns_429() -> None:
    # Token caps are the USD budget's counterpart that still bites for
    # free/local providers — echo's cost_usd is 0, so no USD cap can trigger,
    # but a token cap bounds raw upstream consumption anyway.
    settings = Settings(
        environment="test",
        default_provider="echo",
        api_keys="sk-tokens-a",
        rate_limit_enabled=False,
        client_token_limit=100,
    )
    client = TestClient(create_app(settings))
    try:
        main_module.metrics.record_client_usage(client_id("sk-tokens-a"), tokens=150, cost_usd=0.0)
        body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
        resp = client.post(
            "/v1/chat",
            json=body,
            headers={
                "Authorization": "Bearer sk-tokens-a",
                "Origin": "http://localhost:3000",
            },
        )
        assert resp.status_code == 429
        assert resp.json()["error"] == "token_limit_exceeded"
        assert resp.headers["X-TokenLimit-Remaining"] == "0"
        # CORS is outermost, so this short-circuit 429 is browser-readable too.
        assert "x-tokenlimit-remaining" in resp.headers["access-control-expose-headers"].lower()
    finally:
        main_module.metrics.seed({})


def test_client_token_limit_allows_requests_under_the_cap() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        api_keys="sk-tokens-b",
        rate_limit_enabled=False,
        client_token_limit=10_000,
    )
    client = TestClient(create_app(settings))
    try:
        body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
        resp = client.post("/v1/chat", json=body, headers={"Authorization": "Bearer sk-tokens-b"})
        assert resp.status_code == 200
    finally:
        main_module.metrics.seed({})


def test_client_token_limit_window_enforces_cap_within_window(monkeypatch) -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        api_keys="sk-tokens-w",
        rate_limit_enabled=False,
        client_token_limit=500,
        client_token_limit_window_seconds=100,
    )
    client = TestClient(create_app(settings))
    try:
        monkeypatch.setattr(main_module.time, "time", lambda: 1000.0)
        # Prior window usage recorded in the same window the check will read.
        main_module.metrics.record_client_token_usage(
            client_id("sk-tokens-w"), 600, window_seconds=100, now=1000.0
        )
        body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
        resp = client.post("/v1/chat", json=body, headers={"Authorization": "Bearer sk-tokens-w"})
        assert resp.status_code == 429
        assert resp.json()["error"] == "token_limit_exceeded"
        # Window 10 spans [1000, 1100) -> resets at 1100, retry in 100s.
        assert resp.headers["X-TokenLimit-Reset"] == "1100"
        assert resp.headers["Retry-After"] == "100"

        # Rollover: a request in the next window is allowed again and the new
        # usage was recorded through the response path.
        monkeypatch.setattr(main_module.time, "time", lambda: 1105.0)
        ok = client.post("/v1/chat", json=body, headers={"Authorization": "Bearer sk-tokens-w"})
        assert ok.status_code == 200
        used = main_module.metrics.client_window_tokens(
            client_id("sk-tokens-w"), window_seconds=100, now=1105.0
        )
        assert used == ok.json()["usage"]["total_tokens"]
    finally:
        main_module.metrics.seed({})


def test_client_budget_window_seconds_resets_after_rollover(monkeypatch) -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        api_keys="sk-budget-w2",
        rate_limit_enabled=False,
        client_budget_usd=0.5,
        client_budget_window_seconds=100,
    )
    client = TestClient(create_app(settings))
    try:
        monkeypatch.setattr(main_module.time, "time", lambda: 1000.0)
        main_module.metrics.record_client_budget_usage(
            client_id("sk-budget-w2"), 1.0, window_seconds=100, now=1000.0
        )
        body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
        blocked = client.post(
            "/v1/chat", json=body, headers={"Authorization": "Bearer sk-budget-w2"}
        )
        assert blocked.status_code == 402

        # Move past the window boundary (1100) -> the prior window's spend no
        # longer applies, so the same client is allowed again.
        monkeypatch.setattr(main_module.time, "time", lambda: 1105.0)
        allowed = client.post(
            "/v1/chat", json=body, headers={"Authorization": "Bearer sk-budget-w2"}
        )
        assert allowed.status_code == 200
    finally:
        main_module.metrics.seed({})


def test_client_budget_is_per_client() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        api_keys="sk-budget-over,sk-budget-under",
        rate_limit_enabled=False,
        client_budget_usd=0.5,
    )
    client = TestClient(create_app(settings))
    try:
        main_module.metrics.record_client_usage(
            client_id("sk-budget-over"), tokens=100, cost_usd=1.0
        )
        body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
        over = client.post(
            "/v1/chat", json=body, headers={"Authorization": "Bearer sk-budget-over"}
        )
        under = client.post(
            "/v1/chat", json=body, headers={"Authorization": "Bearer sk-budget-under"}
        )
        assert over.status_code == 402
        assert under.status_code == 200
    finally:
        main_module.metrics.seed({})


def test_client_budget_unset_disables_check() -> None:
    settings = Settings(environment="test", default_provider="echo", rate_limit_enabled=False)
    client = TestClient(create_app(settings))
    try:
        body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
        assert client.post("/v1/chat", json=body).status_code == 200
    finally:
        main_module.metrics.seed({})


def test_client_budget_overrides_parses_key_amount_pairs() -> None:
    settings = Settings(client_budgets_usd="sk-a:5.00, sk-b:20.5")
    assert settings.client_budget_overrides == {"sk-a": 5.00, "sk-b": 20.5}


def test_client_budget_overrides_skips_malformed_entries() -> None:
    settings = Settings(client_budgets_usd="sk-a:oops, no-colon-here, :5.00, sk-b:1.0")
    assert settings.client_budget_overrides == {"sk-b": 1.0}


def test_key_model_allowlists_parses_key_glob_pairs() -> None:
    settings = Settings(key_models="sk-a:gpt-4o*;echo, sk-b:echo, junk-no-colon, :x")
    assert settings.key_model_allowlists == {
        "sk-a": ["gpt-4o*", "echo"],
        "sk-b": ["echo"],
    }


def test_key_model_allowlists_empty_pattern_list_denies_all() -> None:
    # "sk-a:" means the operator listed the key but granted nothing —
    # fail-closed, not silently unrestricted.
    settings = Settings(key_models="sk-a:")
    assert settings.key_model_allowlists == {"sk-a": []}


def _acl_settings(key_models: str = "sk-acl:echo", api_keys: str = "sk-acl,sk-free") -> Settings:
    return Settings(
        environment="test",
        default_provider="echo",
        api_keys=api_keys,
        rate_limit_enabled=False,
        key_models=key_models,
    )


def test_model_acl_denies_unlisted_model() -> None:
    client = TestClient(create_app(_acl_settings()))
    headers = {"Authorization": "Bearer sk-acl"}
    denied = client.post(
        "/v1/chat",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers=headers,
    )
    assert denied.status_code == 403
    assert denied.json()["error"] == "model_not_allowed"
    allowed = client.post(
        "/v1/chat",
        json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]},
        headers=headers,
    )
    assert allowed.status_code == 200


def test_model_acl_denies_models_reached_only_through_fallbacks() -> None:
    client = TestClient(create_app(_acl_settings()))
    resp = client.post(
        "/v1/chat",
        json={
            "model": "echo",
            "messages": [{"role": "user", "content": "hi"}],
            "fallbacks": [{"provider": "ollama", "model": "llama3"}],
        },
        headers={"Authorization": "Bearer sk-acl"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "model_not_allowed"


def test_model_acl_supports_globs_and_ignores_unlisted_keys() -> None:
    client = TestClient(create_app(_acl_settings(key_models="sk-acl:echo*,sk-acl")))
    headers = {"Authorization": "Bearer sk-acl"}
    ok = client.post(
        "/v1/chat",
        json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]},
        headers=headers,
    )
    assert ok.status_code == 200
    # A key with no entry is unrestricted.
    free = client.post(
        "/v1/chat",
        json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer sk-free"},
    )
    assert free.status_code == 200


def test_models_endpoint_is_filtered_by_key_acl() -> None:
    client = TestClient(create_app(_acl_settings()))
    restricted = client.get("/v1/models", headers={"Authorization": "Bearer sk-acl"})
    assert restricted.status_code == 200
    assert {m["id"] for m in restricted.json()["data"]} == {"echo"}
    unrestricted = client.get("/v1/models", headers={"Authorization": "Bearer sk-free"})
    assert {m["id"] for m in unrestricted.json()["data"]} != {"echo"}


def test_model_acl_applies_to_embeddings_and_streaming() -> None:
    client = TestClient(create_app(_acl_settings()))
    headers = {"Authorization": "Bearer sk-acl"}
    emb = client.post(
        "/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": "hi"},
        headers=headers,
    )
    assert emb.status_code == 403
    assert emb.json()["error"] == "model_not_allowed"
    stream = client.post(
        "/v1/chat/stream",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers=headers,
    )
    assert stream.status_code == 403


def test_model_acl_applies_to_anthropic_compat_streaming() -> None:
    client = TestClient(create_app(_acl_settings()))
    headers = {"Authorization": "Bearer sk-acl"}
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert client.post("/v1/messages", json=body, headers=headers).status_code == 403
    stream = client.post("/v1/messages", json={**body, "stream": True}, headers=headers)
    assert stream.status_code == 403
    assert stream.json()["error"]["type"] == "permission_error"


def test_client_budget_override_beats_global_default() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        api_keys="sk-override-tight,sk-override-loose",
        rate_limit_enabled=False,
        client_budget_usd=100.0,  # generous global default
        client_budgets_usd="sk-override-tight:0.5",  # this one key gets a tight cap
    )
    client = TestClient(create_app(settings))
    try:
        main_module.metrics.record_client_usage(
            client_id("sk-override-tight"), tokens=100, cost_usd=1.0
        )
        main_module.metrics.record_client_usage(
            client_id("sk-override-loose"), tokens=100, cost_usd=1.0
        )
        body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
        tight = client.post(
            "/v1/chat", json=body, headers={"Authorization": "Bearer sk-override-tight"}
        )
        loose = client.post(
            "/v1/chat", json=body, headers={"Authorization": "Bearer sk-override-loose"}
        )
        # Same $1.0 spend: the overridden key is over its $0.5 cap, the other
        # is still well under the $100 global default.
        assert tight.status_code == 402
        assert loose.status_code == 200
    finally:
        main_module.metrics.seed({})


def test_metrics_open_by_default_even_with_gateway_auth() -> None:
    settings = Settings(environment="test", api_keys="sk-metrics", rate_limit_enabled=False)
    client = TestClient(create_app(settings))
    assert client.get("/metrics").status_code == 200


def test_metrics_omits_per_client_series_when_unauthenticated() -> None:
    # Open scrape keeps the operational series but not the per-tenant breakdown:
    # rekai_client_cost_usd_total{client="key:…"} names who spent what.
    settings = Settings(
        environment="test", default_provider="echo", api_keys="sk-m", rate_limit_enabled=False
    )
    client = TestClient(create_app(settings))
    client.post(
        "/v1/chat",
        json={"model": "echo", "messages": [{"role": "user", "content": "hi"}], "cache": False},
        headers={"Authorization": "Bearer sk-m"},
    )
    anon = client.get("/metrics")
    assert anon.status_code == 200
    assert "rekai_requests_total" in anon.text  # still scrapeable
    assert "rekai_client_" not in anon.text

    authed = client.get("/metrics", headers={"Authorization": "Bearer sk-m"})
    assert "rekai_client_cost_usd_total" in authed.text


def test_metrics_keeps_per_client_series_without_gateway_auth() -> None:
    # No auth configured -> no tenants to separate; unchanged from before.
    settings = Settings(environment="test", default_provider="echo", rate_limit_enabled=False)
    client = TestClient(create_app(settings))
    client.post(
        "/v1/chat",
        json={"model": "echo", "messages": [{"role": "user", "content": "hi"}], "cache": False},
    )
    assert "rekai_client_requests_total" in client.get("/metrics").text


def test_metrics_require_auth_rejects_missing_key() -> None:
    settings = Settings(
        environment="test",
        api_keys="sk-metrics",
        rate_limit_enabled=False,
        metrics_require_auth=True,
    )
    client = TestClient(create_app(settings))
    resp = client.get("/metrics")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_metrics_require_auth_allows_valid_key() -> None:
    settings = Settings(
        environment="test",
        api_keys="sk-metrics",
        rate_limit_enabled=False,
        metrics_require_auth=True,
    )
    client = TestClient(create_app(settings))
    resp = client.get("/metrics", headers={"Authorization": "Bearer sk-metrics"})
    assert resp.status_code == 200
    assert "rekai_requests_total" in resp.text


def test_metrics_require_auth_is_noop_without_configured_keys() -> None:
    # Nothing to check a Bearer token against, so /metrics stays open — same
    # fallback behaviour as /v1/* with no api_keys configured.
    settings = Settings(environment="test", rate_limit_enabled=False, metrics_require_auth=True)
    client = TestClient(create_app(settings))
    assert client.get("/metrics").status_code == 200


def test_admin_routes_absent_without_admin_key() -> None:
    settings = Settings(environment="test", rate_limit_enabled=False)
    client = TestClient(create_app(settings))
    # Not registered at all — a plain 404, not a 401 (no admin surface to probe).
    assert client.get("/admin/keys").status_code == 404


def test_admin_rejects_missing_or_wrong_key() -> None:
    settings = Settings(environment="test", rate_limit_enabled=False, admin_key="sk-admin-1")
    client = TestClient(create_app(settings))
    assert client.get("/admin/keys").status_code == 401
    resp = client.get("/admin/keys", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_admin_add_disabled_without_dynamic_keys_enabled() -> None:
    settings = Settings(environment="test", rate_limit_enabled=False, admin_key="sk-admin-1")
    client = TestClient(create_app(settings))
    resp = client.post(
        "/admin/keys",
        json={"key": "sk-new"},
        headers={"Authorization": "Bearer sk-admin-1"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "dynamic_keys_disabled"


def test_admin_list_keys_masks_static_and_dynamic() -> None:
    settings = Settings(
        environment="test",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        dynamic_keys_enabled=True,
        api_keys="sk-rekai-abc123",
    )
    client = TestClient(create_app(settings))
    headers = {"Authorization": "Bearer sk-admin-1"}
    client.post("/admin/keys", json={"key": "sk-dyn-longenough"}, headers=headers)
    body = client.get("/admin/keys", headers=headers).json()
    assert body["static"] == ["sk-r…c123"]
    assert body["dynamic"] == ["sk-d…ough"]
    # The raw keys are never returned anywhere.
    assert "sk-rekai-abc123" not in str(body)
    assert "sk-dyn-longenough" not in str(body)


def test_admin_add_key_grants_gateway_access() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        dynamic_keys_enabled=True,
        # No static api_keys: gateway auth is driven entirely by admin-added keys.
    )
    client = TestClient(create_app(settings))
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}

    # Before the key is added, gateway auth is on (dynamic_keys_enabled) and
    # nothing is allowed yet.
    assert client.post("/v1/chat", json=body).status_code == 401

    add = client.post(
        "/admin/keys",
        json={"key": "sk-runtime-key"},
        headers={"Authorization": "Bearer sk-admin-1"},
    )
    assert add.status_code == 201
    assert add.json() == {"status": "added", "key": "sk-r…-key", "expires_at": None}

    resp = client.post("/v1/chat", json=body, headers={"Authorization": "Bearer sk-runtime-key"})
    assert resp.status_code == 200


def test_admin_add_key_with_ttl_expires_out_of_auth(monkeypatch) -> None:
    """expires_in_seconds mints a key that stops working on its own — the
    LiteLLM-style virtual-key TTL for trial tenants / incident access."""
    settings = Settings(
        environment="test",
        default_provider="echo",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        dynamic_keys_enabled=True,
    )
    client = TestClient(create_app(settings))
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
    admin_headers = {"Authorization": "Bearer sk-admin-1"}

    add = client.post(
        "/admin/keys",
        json={"key": "sk-temp-tenant-key", "expires_in_seconds": 3600},
        headers=admin_headers,
    )
    assert add.status_code == 201
    expires_at = add.json()["expires_at"]
    assert expires_at is not None and expires_at > time.time() + 3500

    # Listed with its expiry timestamp, keyed by the masked form.
    listed = client.get("/admin/keys", headers=admin_headers).json()
    assert listed["dynamic"] == ["sk-t…-key"]
    assert listed["dynamic_expires_at"] == {"sk-t…-key": expires_at}

    assert (
        client.post(
            "/v1/chat", json=body, headers={"Authorization": "Bearer sk-temp-tenant-key"}
        ).status_code
        == 200
    )

    # Move the store's clock past expiry — the key no longer authenticates.
    monkeypatch.setattr("rekai.keystore.time", types.SimpleNamespace(time=lambda: expires_at + 1))
    assert (
        client.post(
            "/v1/chat", json=body, headers={"Authorization": "Bearer sk-temp-tenant-key"}
        ).status_code
        == 401
    )
    # …and drops out of the admin listing too.
    listed = client.get("/admin/keys", headers=admin_headers).json()
    assert listed["dynamic"] == []
    assert listed["dynamic_expires_at"] == {}


def test_admin_revoke_key_removes_gateway_access() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        dynamic_keys_enabled=True,
    )
    client = TestClient(create_app(settings))
    admin_headers = {"Authorization": "Bearer sk-admin-1"}
    client.post("/admin/keys", json={"key": "sk-runtime-key"}, headers=admin_headers)
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
    assert (
        client.post(
            "/v1/chat", json=body, headers={"Authorization": "Bearer sk-runtime-key"}
        ).status_code
        == 200
    )

    revoke = client.delete("/admin/keys/sk-runtime-key", headers=admin_headers)
    assert revoke.status_code == 200
    assert revoke.json() == {"status": "revoked", "key": "sk-r…-key", "expires_at": None}

    assert (
        client.post(
            "/v1/chat", json=body, headers={"Authorization": "Bearer sk-runtime-key"}
        ).status_code
        == 401
    )


def test_admin_revoke_unknown_key_returns_404() -> None:
    settings = Settings(
        environment="test",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        dynamic_keys_enabled=True,
    )
    client = TestClient(create_app(settings))
    resp = client.delete(
        "/admin/keys/sk-never-added", headers={"Authorization": "Bearer sk-admin-1"}
    )
    assert resp.status_code == 404


def test_dynamic_keys_encrypted_at_rest_still_grant_access() -> None:
    from rekai.security import generate_key

    settings = Settings(
        environment="test",
        default_provider="echo",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        dynamic_keys_enabled=True,
        dynamic_keys_encryption_key=generate_key(),
    )
    client = TestClient(create_app(settings))
    client.post(
        "/admin/keys",
        json={"key": "sk-encrypted-demo"},
        headers={"Authorization": "Bearer sk-admin-1"},
    )
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
    resp = client.post("/v1/chat", json=body, headers={"Authorization": "Bearer sk-encrypted-demo"})
    assert resp.status_code == 200


@pytest.fixture
def admin_audit_log(caplog):
    """Attach caplog's handler directly to the ``rekai.admin`` logger.

    create_app() -> configure_logging() clears the *root* logger's handlers on
    every call (by design, so repeated app creation in a long-lived process
    doesn't stack handlers) — which also strips pytest's caplog handler if it
    was attached there. Attaching directly to the named logger sidesteps that.
    """
    logger = logging.getLogger("rekai.admin")
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


def test_admin_audit_log_records_auth_failure(admin_audit_log) -> None:
    settings = Settings(environment="test", rate_limit_enabled=False, admin_key="sk-admin-1")
    client = TestClient(create_app(settings))
    client.get("/admin/keys", headers={"Authorization": "Bearer wrong"})
    records = [r for r in admin_audit_log.records if r.name == "rekai.admin"]
    assert len(records) == 1
    assert records[0].admin_action == "auth_failed"
    assert records[0].path == "/admin/keys"
    # The wrong key itself is never logged, only the fact that auth failed.
    assert "wrong" not in admin_audit_log.text


def test_admin_audit_log_records_add_with_masked_key(admin_audit_log) -> None:
    settings = Settings(
        environment="test",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        dynamic_keys_enabled=True,
    )
    client = TestClient(create_app(settings))
    client.post(
        "/admin/keys",
        json={"key": "sk-audit-secret-value"},
        headers={"Authorization": "Bearer sk-admin-1"},
    )
    records = [r for r in admin_audit_log.records if r.name == "rekai.admin"]
    assert len(records) == 1
    assert records[0].admin_action == "add_key"
    assert records[0].key == "sk-a…alue"
    # The raw key is never written to the audit log.
    assert "sk-audit-secret-value" not in admin_audit_log.text


def test_admin_audit_log_records_revoke_and_not_found(admin_audit_log) -> None:
    settings = Settings(
        environment="test",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        dynamic_keys_enabled=True,
    )
    client = TestClient(create_app(settings))
    admin_headers = {"Authorization": "Bearer sk-admin-1"}
    client.post("/admin/keys", json={"key": "sk-to-revoke"}, headers=admin_headers)

    client.delete("/admin/keys/sk-to-revoke", headers=admin_headers)
    client.delete("/admin/keys/sk-never-existed", headers=admin_headers)

    records = [r for r in admin_audit_log.records if r.name == "rekai.admin"]
    actions = [r.admin_action for r in records]
    assert "revoke_key" in actions
    assert "revoke_key_not_found" in actions


def test_admin_audit_log_records_list(admin_audit_log) -> None:
    settings = Settings(environment="test", rate_limit_enabled=False, admin_key="sk-admin-1")
    client = TestClient(create_app(settings))
    client.get("/admin/keys", headers={"Authorization": "Bearer sk-admin-1"})
    records = [r for r in admin_audit_log.records if r.name == "rekai.admin"]
    assert len(records) == 1
    assert records[0].admin_action == "list_keys"


def test_admin_rate_limit_blocks_after_capacity() -> None:
    settings = Settings(
        environment="test",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        admin_rate_limit_requests=2,
        admin_rate_limit_window_seconds=60,
    )
    client = TestClient(create_app(settings))
    headers = {"Authorization": "Bearer sk-admin-1"}
    assert client.get("/admin/keys", headers=headers).status_code == 200
    assert client.get("/admin/keys", headers=headers).status_code == 200
    blocked = client.get("/admin/keys", headers=headers)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1


def test_admin_rate_limit_counts_failed_auth_attempts() -> None:
    # Unlike the tenant gateway-auth gate (auth checked before rate limiting,
    # so a guesser can't burn a real tenant's budget), the admin gate counts
    # every attempt — right or wrong key — since the threat here is
    # brute-forcing the one shared secret, not fairness between tenants.
    settings = Settings(
        environment="test",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        admin_rate_limit_requests=2,
        admin_rate_limit_window_seconds=60,
    )
    client = TestClient(create_app(settings))
    wrong = {"Authorization": "Bearer wrong-guess"}
    assert client.get("/admin/keys", headers=wrong).status_code == 401
    assert client.get("/admin/keys", headers=wrong).status_code == 401
    # The 3rd attempt is rate limited even with the *correct* key now, because
    # the two wrong guesses already consumed the shared IP budget.
    resp = client.get("/admin/keys", headers={"Authorization": "Bearer sk-admin-1"})
    assert resp.status_code == 429


def test_admin_rate_limit_can_be_disabled() -> None:
    settings = Settings(
        environment="test",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        admin_rate_limit_enabled=False,
        admin_rate_limit_requests=1,
    )
    client = TestClient(create_app(settings))
    headers = {"Authorization": "Bearer sk-admin-1"}
    for _ in range(5):
        assert client.get("/admin/keys", headers=headers).status_code == 200


# --- rate limiter: bucket eviction under a flood ------------------------------
# The bucket cap exists so a flood of distinct client ids can't grow memory
# without bound. Reclaiming *only* fully-refilled buckets silently failed in
# exactly that case — during a flood every bucket is mid-refill, so nothing was
# prunable, the dict grew past the cap, and the O(n) scan then ran on every
# subsequent request. Measured before the fix: 8000 distinct ids against the
# default 60-per-60s config took 4.4s and left 1612 buckets under a 1000 cap —
# the limiter amplifying the abuse it exists to stop (an algorithmic-complexity
# attack, Crosby & Wallach, USENIX Security 2003).


def _flood(limiter: RateLimiter, count: int, prefix: str = "ip") -> None:
    for i in range(count):
        limiter.allow(f"{prefix}-{i}")


def test_bucket_count_stays_capped_when_nothing_can_refill() -> None:
    # A long window means a bucket that spent one token needs 360s to refill,
    # so the old fully-refilled-only policy could reclaim nothing at all.
    limiter = RateLimiter(capacity=10, window=3600.0, max_buckets=200)
    _flood(limiter, 5000)
    assert len(limiter._buckets) <= 200


def test_bucket_count_stays_capped_on_the_default_config() -> None:
    limiter = RateLimiter(capacity=60, window=60.0, max_buckets=1000)
    _flood(limiter, 8000)
    assert len(limiter._buckets) <= 1000


def test_reclaim_is_amortized_not_run_per_request() -> None:
    # The cost that made this quadratic: once at the cap, every single request
    # paid a full scan. Batched eviction must make that ~1 pass per 10% of the
    # cap, not one per admission.
    limiter = RateLimiter(capacity=10, window=3600.0, max_buckets=100)
    passes = 0
    original = limiter._reclaim

    def counting(now: float) -> None:
        nonlocal passes
        passes += 1
        original(now)

    limiter._reclaim = counting  # type: ignore[method-assign]
    _flood(limiter, 1000)
    # ~900 admissions happen past the cap; one pass each would be ~900.
    assert passes < 200


def test_eviction_never_hands_budget_back_to_a_throttled_client() -> None:
    # Eviction resets a client to full capacity, so it *grants* budget. If the
    # policy evicted the most-throttled buckets, an attacker could flood
    # distinct keys to force their own exhausted bucket out and reset their
    # limit — turning the cap into a rate-limit bypass.
    limiter = RateLimiter(capacity=5, window=3600.0, max_buckets=50)
    for _ in range(5):
        limiter.allow("victim")
    assert limiter.allow("victim") is False  # budget exhausted

    _flood(limiter, 500, prefix="flood")

    assert limiter.allow("victim") is False  # still exhausted, not reset
    assert limiter.remaining("victim") == 0


def test_idle_buckets_are_still_reclaimed_first() -> None:
    # The cheap case must keep working: a fully-refilled bucket carries no
    # state, so it goes before any partially-spent one.
    limiter = RateLimiter(capacity=5, window=1.0, max_buckets=10)
    limiter.allow("idle")
    time.sleep(1.1)  # "idle" refills completely
    for i in range(9):
        limiter.allow(f"active-{i}")
    limiter.allow("trigger")
    assert "idle" not in limiter._buckets


def test_a_normal_client_is_unaffected_by_the_cap() -> None:
    # Sanity: ordinary traffic still gets exactly its configured budget.
    limiter = RateLimiter(capacity=3, window=3600.0, max_buckets=1000)
    assert [limiter.allow("solo") for _ in range(4)] == [True, True, True, False]
