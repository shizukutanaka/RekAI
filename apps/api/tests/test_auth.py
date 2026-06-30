"""Tests for optional gateway API-key authentication."""

from __future__ import annotations

from fastapi.testclient import TestClient

from rekai.auth import client_id, key_allowed, parse_bearer
from rekai.config import Settings
from rekai.main import create_app


def test_client_id_is_masked_and_stable() -> None:
    cid = client_id("sk-secret")
    assert cid.startswith("key:")
    assert "sk-secret" not in cid  # never the raw key
    assert client_id("sk-secret") == cid  # stable
    assert client_id("sk-other") != cid


def test_parse_bearer() -> None:
    assert parse_bearer("Bearer sk-123") == "sk-123"
    assert parse_bearer("bearer sk-123") == "sk-123"  # scheme is case-insensitive
    assert parse_bearer("Basic sk-123") is None
    assert parse_bearer("sk-123") is None
    assert parse_bearer("Bearer ") is None
    assert parse_bearer(None) is None


def test_key_allowed() -> None:
    assert key_allowed("k2", ["k1", "k2", "k3"]) is True
    assert key_allowed("nope", ["k1", "k2"]) is False
    assert key_allowed("k1", []) is False


def _chat(client: TestClient, **headers: str):
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
    return client.post("/v1/chat", json=body, headers=headers)


def test_open_when_no_keys_configured() -> None:
    client = TestClient(create_app(Settings(environment="test", default_provider="echo")))
    assert _chat(client).status_code == 200  # no auth required


def test_requires_valid_key_when_configured() -> None:
    settings = Settings(environment="test", default_provider="echo", api_keys="sk-a, sk-b")
    client = TestClient(create_app(settings))

    assert _chat(client).status_code == 401  # missing
    assert _chat(client, Authorization="Bearer wrong").status_code == 401  # invalid
    assert _chat(client, Authorization="Bearer sk-b").status_code == 200  # valid
    # The challenge header is present on a 401.
    assert _chat(client).headers["WWW-Authenticate"] == "Bearer"


def test_rate_limit_is_per_key() -> None:
    # 1 request per window; two keys should each get their own budget.
    settings = Settings(
        environment="test",
        default_provider="echo",
        api_keys="sk-a,sk-b",
        rate_limit_enabled=True,
        rate_limit_requests=1,
        rate_limit_window_seconds=60,
    )
    client = TestClient(create_app(settings))
    assert _chat(client, Authorization="Bearer sk-a").status_code == 200  # a: 1st ok
    assert _chat(client, Authorization="Bearer sk-a").status_code == 429  # a: over budget
    assert _chat(client, Authorization="Bearer sk-b").status_code == 200  # b: own budget


def test_health_stays_open_with_keys_configured() -> None:
    settings = Settings(environment="test", api_keys="sk-a")
    client = TestClient(create_app(settings))
    # System endpoints aren't under /v1 -> no auth (liveness probes, scraping).
    assert client.get("/health").status_code == 200
    assert client.get("/metrics").status_code == 200
