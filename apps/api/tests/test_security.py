import pytest
from fastapi.testclient import TestClient

import rekai.main as main_module
from rekai.auth import client_id
from rekai.config import Settings
from rekai.main import create_app
from rekai.providers import register_provider
from rekai.providers.base import Provider, ProviderError
from rekai.rate_limit import RateLimiter
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


def test_rate_limiter_prunes_idle_buckets() -> None:
    import time

    # Active clients (below capacity) are retained even when the cap is hit, so
    # legitimate traffic keeps its budget.
    limiter = RateLimiter(capacity=5, window=60, max_buckets=10)
    for i in range(11):
        limiter.allow(f"active-{i}")  # each consumes a token -> not full -> kept
    assert len(limiter._buckets) == 11

    # _prune drops fully-refilled (idle) buckets but keeps active ones.
    now = time.time()
    limiter._buckets["idle"] = (5.0, now)  # tokens == capacity -> idle
    limiter._buckets["busy"] = (0.5, now)  # partially spent -> active
    limiter._prune(now)
    assert "idle" not in limiter._buckets
    assert "busy" in limiter._buckets


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
