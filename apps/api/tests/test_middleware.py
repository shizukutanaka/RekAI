"""Tests for request-id and latency middleware."""

from __future__ import annotations

from fastapi.testclient import TestClient

from rekai.config import Settings
from rekai.main import create_app


def test_oversized_body_rejected() -> None:
    settings = Settings(environment="test", default_provider="echo", max_body_bytes=200)
    client = TestClient(create_app(settings))
    big = {"model": "echo", "messages": [{"role": "user", "content": "x" * 500}]}
    resp = client.post("/v1/chat", json=big)
    assert resp.status_code == 413
    assert resp.json()["error"] == "payload_too_large"
    # A small body still goes through.
    small = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
    assert client.post("/v1/chat", json=small).status_code == 200


def test_body_limit_disabled_with_zero() -> None:
    settings = Settings(environment="test", default_provider="echo", max_body_bytes=0)
    client = TestClient(create_app(settings))
    big = {"model": "echo", "messages": [{"role": "user", "content": "x" * 5000}]}
    assert client.post("/v1/chat", json=big).status_code == 200


def test_request_id_generated(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    rid = resp.headers.get("X-Request-ID")
    assert rid and len(rid) >= 8
    assert "X-Response-Time-Ms" in resp.headers
    # Latency header parses as a float.
    float(resp.headers["X-Response-Time-Ms"])


def test_version_header(client: TestClient) -> None:
    from rekai import __version__

    resp = client.get("/health")
    assert resp.headers["X-RekAI-Version"] == __version__


def test_request_id_propagated(client: TestClient) -> None:
    resp = client.get("/health", headers={"X-Request-ID": "my-trace-123"})
    assert resp.headers["X-Request-ID"] == "my-trace-123"


def test_request_id_present_on_errors(client: TestClient) -> None:
    # Unknown provider -> 400, but the request id header is still set.
    resp = client.post(
        "/v1/chat",
        json={
            "model": "x",
            "provider": "nope",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 400
    assert "X-Request-ID" in resp.headers
