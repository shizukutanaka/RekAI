from fastapi.testclient import TestClient


def test_chat_echo(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat",
        json={"model": "echo", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "echo"
    assert body["content"] == "Echo: hello"
    assert body["cached"] is False
    assert body["usage"]["total_tokens"] > 0


def test_chat_is_cached_on_second_call(client: TestClient) -> None:
    payload = {"model": "echo", "messages": [{"role": "user", "content": "cache me"}]}
    first = client.post("/v1/chat", json=payload)
    second = client.post("/v1/chat", json=payload)
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert first.json()["content"] == second.json()["content"]


def test_chat_cache_disabled_per_request(client: TestClient) -> None:
    payload = {
        "model": "echo",
        "messages": [{"role": "user", "content": "no cache"}],
        "cache": False,
    }
    client.post("/v1/chat", json=payload)
    second = client.post("/v1/chat", json=payload)
    assert second.json()["cached"] is False


def test_chat_unknown_provider(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat",
        json={
            "model": "whatever",
            "provider": "does-not-exist",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "provider_error"


def test_chat_validation_error(client: TestClient) -> None:
    resp = client.post("/v1/chat", json={"model": "echo", "messages": []})
    assert resp.status_code == 422


def test_openai_requires_key(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat",
        json={
            "provider": "openai",
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 401


def test_models_listing(client: TestClient) -> None:
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    ids = {m["id"] for m in resp.json()["data"]}
    assert "echo" in ids
    assert "gpt-4o-mini" in ids
