import pytest
from fastapi.testclient import TestClient

from rekai.config import Settings
from rekai.main import create_app
from rekai.rate_limit import RateLimiter
from rekai.security import KeyCipher, generate_key, mask_key


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
    blocked = client.post("/v1/chat", json=body)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert blocked.json()["error"] == "rate_limited"
