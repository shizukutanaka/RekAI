"""REKAI_CLIENT_RATE_LIMITS — per-key request ceilings.

The global REKAI_RATE_LIMIT_REQUESTS applies the same ceiling to every
tenant; overrides let an operator sell a higher tier or throttle one noisy
key, keyed on the raw API key (same convention as client_budgets_usd).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from rekai.config import Settings
from rekai.main import create_app
from rekai.rate_limit import RateLimiter


def _settings(**kw) -> Settings:
    kw.setdefault("environment", "test")
    kw.setdefault("default_provider", "echo")
    return Settings(**kw)


def _client(**kw) -> TestClient:
    return TestClient(create_app(_settings(**kw)))


def test_overrides_parse():
    s = _settings(client_rate_limits="sk-a:100, bad, sk-b:0, sk-c:xyz, :5, sk-d:7")
    assert s.client_rate_limit_overrides == {"sk-a": 100, "sk-d": 7}


def test_per_key_ceiling_enforced_e2e() -> None:
    client = _client(api_keys="sk-a,sk-b", client_rate_limits="sk-b:1")
    ok = client.post(
        "/v1/chat",
        json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer sk-b"},
    )
    assert ok.status_code == 200
    assert ok.headers["X-RateLimit-Limit"] == "1"
    limited = client.post(
        "/v1/chat",
        json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer sk-b"},
    )
    assert limited.status_code == 429
    assert limited.json()["error"] == "rate_limited"


def test_unlisted_key_uses_global_limit() -> None:
    client = _client(api_keys="sk-a,sk-b", client_rate_limits="sk-b:1", rate_limit_requests=60)
    for _ in range(2):
        resp = client.post(
            "/v1/chat",
            json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer sk-a"},
        )
        assert resp.status_code == 200
        assert resp.headers["X-RateLimit-Limit"] == "60"


def test_override_higher_than_global_is_honored() -> None:
    # A bigger tier, not just a smaller one: sk-a gets 3 while global is 1.
    client = _client(api_keys="sk-a", client_rate_limits="sk-a:3", rate_limit_requests=1)
    codes = [
        client.post(
            "/v1/chat",
            json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer sk-a"},
        ).status_code
        for _ in range(4)
    ]
    assert codes == [200, 200, 200, 429]


def test_token_bucket_respects_per_key_capacity() -> None:
    limiter = RateLimiter(capacity=60, window=60)
    assert limiter.allow("a", capacity=2) is True
    assert limiter.allow("a", capacity=2) is True
    assert limiter.allow("a", capacity=2) is False
    # The default-capacity neighbour is unaffected.
    assert limiter.allow("b") is True


def test_retry_after_scales_with_capacity() -> None:
    limiter = RateLimiter(capacity=60, window=60)
    limiter.allow("a", capacity=1)
    assert limiter.allow("a", capacity=1) is False
    # One token per window at cap 1: ~60s until refill.
    assert limiter.retry_after("a", capacity=1) >= 55
