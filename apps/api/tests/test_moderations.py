"""Tests for POST /v1/moderations.

The endpoint mirrors OpenAI's `/v1/moderations`: request/response shapes pass
through (including the content-part input form), providers without a
moderation endpoint answer 400, and echo returns a deterministic unflagged
result so the surface works keyless.
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from rekai.providers.echo import EchoProvider
from rekai.providers.openai import OpenAIProvider

SECRET = "sk-proj-" + "A" * 40


class _Resp:
    status_code = 200
    headers: dict = {}
    text = ""

    def json(self) -> dict:
        return {
            "id": "modr-upstream",
            "model": "omni-moderation-latest",
            "results": [
                {
                    "flagged": True,
                    "categories": {"harassment": True},
                    "category_scores": {"harassment": 0.9},
                }
            ],
        }


class _Client:
    captured: dict = {}
    posted_url: str = ""

    def __init__(self, *a: object, **k: object) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a: object):
        return False

    async def aclose(self) -> None:
        return None

    async def post(self, url, json=None, headers=None, **kw):
        _Client.posted_url = url
        _Client.captured = json or {}
        return _Resp()


def test_moderations_echo_string_input(client: TestClient) -> None:
    # omni-moderation-* routes to openai by name, so echo must be explicit.
    resp = client.post("/v1/moderations", json={"input": "hello", "provider": "echo"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "echo"
    # model omitted -> OpenAI's own default is used.
    assert body["model"] == "omni-moderation-latest"
    assert body["id"].startswith("modr-echo-")
    assert body["results"] == [{"flagged": False, "categories": {}, "category_scores": {}}]


def test_moderations_echo_list_input(client: TestClient) -> None:
    resp = client.post("/v1/moderations", json={"input": ["a", "b"], "provider": "echo"})
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 2


async def test_echo_moderate_deterministic() -> None:
    a = await EchoProvider().moderate(["x"], "m", None)
    b = await EchoProvider().moderate(["x"], "m", None)
    assert a == b


def test_moderations_unsupported_provider(client: TestClient) -> None:
    resp = client.post("/v1/moderations", json={"input": "x", "provider": "anthropic"})
    assert resp.status_code == 400
    # OpenAI-compat surface: errors arrive in OpenAI's envelope, not `detail`.
    assert "does not support moderation" in resp.json()["error"]["message"]


def test_moderations_validation_error_uses_openai_envelope(client: TestClient) -> None:
    resp = client.post("/v1/moderations", json={})
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert "input" in error["message"]
    assert error["param"] == "input"


async def test_moderation_reaches_openai_verbatim(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    parts = [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "u"}}]
    result = await OpenAIProvider().moderate(parts, "omni-moderation-latest", "sk-x")
    assert _Client.posted_url.endswith("/moderations")
    assert _Client.captured == {"input": parts, "model": "omni-moderation-latest"}
    assert result.id == "modr-upstream"
    assert result.results[0]["flagged"] is True


def test_moderations_route_surfaces_upstream_result(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    before = client.get("/v1/usage").json()
    resp = client.post(
        "/v1/moderations",
        json={"input": "x", "provider": "openai", "model": "m1"},
        headers={"X-Provider-Key": "sk-byok"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "openai"
    assert body["id"] == "modr-upstream"
    assert body["results"][0]["categories"] == {"harassment": True}
    # A moderation call counts toward usage_by_model and usage_by_client as a
    # request (no tokens).
    usage = client.get("/v1/usage").json()
    assert usage["usage_by_model"]["omni-moderation-latest"]["requests"] >= 1
    total_client_requests = sum(u["requests"] for u in usage["usage_by_client"].values())
    assert (
        total_client_requests == sum(u["requests"] for u in before["usage_by_client"].values()) + 1
    )


def test_input_secrets_scan_moderation_input() -> None:
    from rekai.config import Settings
    from rekai.main import create_app

    guarded = TestClient(
        create_app(
            Settings(
                environment="test",
                default_provider="echo",
                rate_limit_enabled=False,
                input_secrets_enabled=True,
                guardrails_action="block",
            )
        )
    )
    resp = guarded.post("/v1/moderations", json={"input": SECRET})
    assert resp.status_code == 403
    assert "credential" in resp.json()["error"]["message"]
    # ...and inside a content-part list too.
    resp = guarded.post("/v1/moderations", json={"input": [{"type": "text", "text": SECRET}]})
    assert resp.status_code == 403
