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


def test_oversized_chunked_body_rejected() -> None:
    """A body sent without Content-Length (chunked) must still hit the cap."""
    import json as jsonlib

    settings = Settings(environment="test", default_provider="echo", max_body_bytes=200)
    client = TestClient(create_app(settings))
    big = jsonlib.dumps(
        {"model": "echo", "messages": [{"role": "user", "content": "x" * 500}]}
    ).encode()

    def body_chunks():
        # httpx sends an iterator body as Transfer-Encoding: chunked (no
        # Content-Length header), which skips the middleware's header check.
        for i in range(0, len(big), 64):
            yield big[i : i + 64]

    resp = client.post(
        "/v1/chat",
        content=body_chunks(),
        headers={"Content-Type": "application/json", "Origin": "http://example.com"},
    )
    assert resp.status_code == 413
    assert resp.json()["error"] == "payload_too_large"
    # CORS wraps MaxBodySizeMiddleware, so even this short-circuited response
    # carries CORS headers (the browser can read the 413 instead of seeing a
    # failed fetch).
    assert resp.headers.get("access-control-allow-origin") == "*"


def test_small_chunked_body_accepted() -> None:
    import json as jsonlib

    settings = Settings(environment="test", default_provider="echo", max_body_bytes=10_000)
    client = TestClient(create_app(settings))
    body = jsonlib.dumps(
        {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
    ).encode()

    def body_chunks():
        yield body[: len(body) // 2]
        yield body[len(body) // 2 :]

    resp = client.post(
        "/v1/chat",
        content=body_chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200


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


def test_nosniff_header(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


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
