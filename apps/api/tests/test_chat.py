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
    # echo is a free provider -> cost is exactly 0.0 (not null).
    assert body["cost_usd"] == 0.0


def test_usage_summary_accumulates(client: TestClient) -> None:
    before = client.get("/v1/usage").json()["requests_total"]
    client.post(
        "/v1/chat",
        json={"model": "echo", "messages": [{"role": "user", "content": "count me"}]},
    )
    after = client.get("/v1/usage").json()
    assert after["requests_total"] == before + 1
    assert "echo" in after["requests_by_provider"]
    assert isinstance(after["cost_usd_total"], (int, float))


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


def test_models_include_pricing(client: TestClient) -> None:
    by_id = {m["id"]: m for m in client.get("/v1/models").json()["data"]}
    # A priced model exposes its per-1M rates from the pricing table.
    assert by_id["gpt-4o-mini"]["pricing"] == {"input_per_1m": 0.15, "output_per_1m": 0.60}
    # An unpriced/free model reports null pricing.
    assert by_id["echo"]["pricing"] is None


def test_pricing_overrides_flow_through_to_v1_models() -> None:
    from rekai.config import Settings
    from rekai.main import create_app

    settings = Settings(
        environment="test",
        rate_limit_enabled=False,
        pricing_overrides="gpt-4o-mini:1.00:2.00",
    )
    local_client = TestClient(create_app(settings))
    by_id = {m["id"]: m for m in local_client.get("/v1/models").json()["data"]}
    # Overriding an existing prefix replaces its price for this deployment...
    assert by_id["gpt-4o-mini"]["pricing"] == {"input_per_1m": 1.00, "output_per_1m": 2.00}
    # ...without touching the global table other Settings instances (and other
    # models in this same response) see.
    assert by_id["gpt-4o"]["pricing"] == {"input_per_1m": 2.50, "output_per_1m": 10.00}
