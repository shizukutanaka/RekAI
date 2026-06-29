from fastapi.testclient import TestClient


def test_root_banner(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "RekAI"
    assert body["docs"] == "/docs"
    assert body["health"] == "/health"
    assert body["version"]


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "echo" in body["providers"]
    assert body["cache"] in {"memory", "redis", "disabled"}


def test_health_provider_status(client: TestClient) -> None:
    status = client.get("/health").json()["provider_status"]
    # Keyless providers are ready out of the box.
    assert status["echo"] == "ready"
    assert status["ollama"] == "ready"
    # Key-requiring providers without a server key need BYOK (test env has none).
    assert status["openai"] == "byok_only"
    assert status["anthropic"] == "byok_only"
    assert status["gemini"] == "byok_only"


def test_metrics_endpoint(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "rekai_requests_total" in resp.text


def test_openapi_schema(client: TestClient) -> None:
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    assert "/v1/chat" in schema["paths"]
    assert schema["info"]["title"] == "RekAI"
