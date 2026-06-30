"""Tests for Idempotency-Key replay on the chat/embeddings endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from rekai.config import Settings
from rekai.main import create_app


def _client() -> TestClient:
    # Memory cache (default) backs the idempotency store.
    return TestClient(create_app(Settings(environment="test", default_provider="echo")))


def test_chat_idempotency_key_replays_first_response() -> None:
    client = _client()
    body = {"model": "echo", "messages": [{"role": "user", "content": "hello"}]}
    headers = {"Idempotency-Key": "abc-123"}

    first = client.post("/v1/chat", json=body, headers=headers)
    assert first.status_code == 200
    assert first.headers.get("Idempotent-Replay") is None

    # Same key, even with a *different* body, returns the stored first response.
    other = {"model": "echo", "messages": [{"role": "user", "content": "totally different"}]}
    second = client.post("/v1/chat", json=other, headers=headers)
    assert second.status_code == 200
    assert second.headers["Idempotent-Replay"] == "true"
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["content"] == first.json()["content"]


def test_chat_without_key_is_not_deduped() -> None:
    client = _client()
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}], "cache": False}
    r1 = client.post("/v1/chat", json=body)
    r2 = client.post("/v1/chat", json=body)
    # No idempotency key and caching off -> fresh ids each time.
    assert r1.json()["id"] != r2.json()["id"]


def test_different_keys_process_independently() -> None:
    client = _client()
    # cache:false isolates idempotency from the content cache.
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}], "cache": False}
    a = client.post("/v1/chat", json=body, headers={"Idempotency-Key": "k1"})
    b = client.post("/v1/chat", json=body, headers={"Idempotency-Key": "k2"})
    assert b.headers.get("Idempotent-Replay") is None
    assert a.json()["id"] != b.json()["id"]


def test_embeddings_idempotency_key_replays() -> None:
    client = _client()
    headers = {"Idempotency-Key": "emb-1"}
    first = client.post("/v1/embeddings", json={"model": "echo", "input": "x"}, headers=headers)
    second = client.post(
        "/v1/embeddings", json={"model": "echo", "input": "different"}, headers=headers
    )
    assert second.headers["Idempotent-Replay"] == "true"
    assert second.json()["embeddings"] == first.json()["embeddings"]
