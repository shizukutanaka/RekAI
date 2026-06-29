"""Tests for request-id and latency middleware."""

from __future__ import annotations

from fastapi.testclient import TestClient


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
