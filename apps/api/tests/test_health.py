import time

from fastapi.testclient import TestClient

from rekai.config import Settings
from rekai.main import create_app


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


def _prod_client(**kw):
    kw.setdefault("environment", "production")
    kw.setdefault("default_provider", "echo")
    # The open-proxy guard refuses production startup without a provider key
    # only when a server-side key is configured — echo needs none.
    return TestClient(create_app(Settings(**kw)), raise_server_exceptions=False)


def test_docs_are_off_in_production() -> None:
    """A public Swagger UI hands an attacker the schema of every route,
    /admin/* included — so production drops the whole OpenAPI surface."""
    c = _prod_client()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404, path
    assert c.get("/").json()["docs"] is None


def test_docs_enabled_overrides_the_production_default() -> None:
    c = _prod_client(docs_enabled=True)
    assert c.get("/openapi.json").status_code == 200
    assert c.get("/").json()["docs"] == "/docs"


def test_docs_enabled_false_hides_them_in_development(client: TestClient) -> None:
    # `client` runs the test environment where docs default on.
    assert client.get("/openapi.json").status_code == 200
    c = _prod_client(environment="test", docs_enabled=False)
    assert c.get("/openapi.json").status_code == 404


def test_openapi_documents_error_responses(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    for path in ("/v1/chat", "/v1/embeddings", "/v1/chat/stream"):
        responses = paths[path]["post"]["responses"]
        # The rate-limit and body-size errors are part of the documented contract.
        assert "413" in responses
        assert "429" in responses


# --- degraded state ----------------------------------------------------------
# The cooldown/circuit-breaker machinery already knew which providers were
# parked; /health was typed Literal["ok"] and could never say so.


def test_health_reports_degraded_while_a_provider_is_parked(client: TestClient) -> None:
    from rekai.cooldown import cooldowns

    cooldowns.clear()
    try:
        assert client.get("/health").json()["status"] == "ok"

        cooldowns.mark("openai", 30.0)
        body = client.get("/health").json()
        assert body["status"] == "degraded"
        assert 0 < body["parked_providers"]["openai"] <= 30.0
        # Degraded is still 200: the gateway is serving, just not from every
        # backend, and failing an orchestrator's liveness probe over one parked
        # provider would take down a working deployment.
        assert client.get("/health").status_code == 200
    finally:
        cooldowns.clear()


def test_health_recovers_when_the_cooldown_expires(client: TestClient) -> None:
    from rekai.cooldown import cooldowns

    cooldowns.clear()
    cooldowns.mark("openai", 0.01)
    time.sleep(0.05)
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["parked_providers"] == {}


def test_parked_drops_expired_entries() -> None:
    from rekai.cooldown import Cooldown

    clock = iter([0.0, 0.0, 100.0])
    cd = Cooldown(clock=lambda: next(clock))
    cd.mark("a", 10.0)  # expires at 10
    assert set(cd.parked()) == {"a"}
    assert cd.parked() == {}  # clock now 100
