"""Tests for the embeddings endpoint and providers."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from rekai.providers.base import ProviderError
from rekai.providers.echo import EchoProvider
from rekai.providers.gemini import GeminiProvider
from rekai.providers.ollama import OllamaProvider
from rekai.providers.openai import OpenAIProvider


async def test_echo_embeddings_deterministic() -> None:
    a = await EchoProvider().embed(["hello", "world"], "echo", None)
    b = await EchoProvider().embed(["hello", "world"], "echo", None)
    assert len(a.embeddings) == 2
    assert all(len(v) == 16 for v in a.embeddings)
    assert a.embeddings == b.embeddings  # deterministic
    assert a.embeddings[0] != a.embeddings[1]  # different inputs -> different vectors


def test_embeddings_endpoint_string_input(client: TestClient) -> None:
    resp = client.post("/v1/embeddings", json={"model": "echo", "input": "hello"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "echo"
    assert len(body["embeddings"]) == 1
    assert len(body["embeddings"][0]) == 16
    assert body["cached"] is False


def test_embeddings_endpoint_list_input(client: TestClient) -> None:
    resp = client.post("/v1/embeddings", json={"model": "echo", "input": ["a", "b", "c"]})
    assert resp.status_code == 200
    assert len(resp.json()["embeddings"]) == 3


def test_embeddings_cached_on_second_call(client: TestClient) -> None:
    payload = {"model": "echo", "input": "cache this embedding"}
    first = client.post("/v1/embeddings", json=payload)
    second = client.post("/v1/embeddings", json=payload)
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert first.json()["embeddings"] == second.json()["embeddings"]


def test_embeddings_unsupported_provider(client: TestClient) -> None:
    # ollama is reachable in routing but has no embed() override here -> 400.
    resp = client.post(
        "/v1/embeddings",
        json={"provider": "anthropic", "model": "x", "input": "hi"},
    )
    assert resp.status_code == 400
    assert "does not support embeddings" in resp.json()["detail"]


def test_embeddings_endpoint_reports_cost(client: TestClient) -> None:
    # echo is a free provider, so cost is 0.0 (not None).
    resp = client.post("/v1/embeddings", json={"model": "echo", "input": "cost me"})
    assert resp.status_code == 200
    assert resp.json()["cost_usd"] == 0.0


def test_embedding_pricing_input_only() -> None:
    from rekai.pricing import estimate_cost
    from rekai.schemas import Usage

    usage = Usage(prompt_tokens=1000, completion_tokens=0, total_tokens=1000)
    cost = estimate_cost("openai", "text-embedding-3-small", usage)
    assert cost == round(1000 * 0.02 / 1_000_000, 6)


async def test_default_provider_raises_for_embeddings() -> None:
    with pytest.raises(ProviderError):
        await OpenAIProvider().embed(["hi"], "text-embedding-3-small", api_key=None)


async def test_openai_embeddings_parsed(monkeypatch) -> None:
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {
                "model": "text-embedding-3-small",
                "data": [
                    {"index": 1, "embedding": [0.4, 0.5]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ],
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
            }

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    result = await OpenAIProvider().embed(["x", "y"], "text-embedding-3-small", api_key="sk-test")
    # rows re-ordered by index.
    assert result.embeddings == [[0.1, 0.2], [0.4, 0.5]]
    assert captured["url"].endswith("/embeddings")
    assert captured["json"]["input"] == ["x", "y"]


async def test_ollama_embeddings_parsed(monkeypatch) -> None:
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {
                "model": "nomic-embed-text",
                "embeddings": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]],
                "prompt_eval_count": 5,
            }

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    result = await OllamaProvider().embed(["x", "y"], "nomic-embed-text", api_key=None)
    assert result.embeddings == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    assert result.usage.prompt_tokens == 5
    assert captured["url"].endswith("/api/embed")
    assert captured["json"] == {"model": "nomic-embed-text", "input": ["x", "y"]}


async def test_gemini_embeddings_parsed(monkeypatch) -> None:
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {
                "embeddings": [
                    {"values": [0.1, 0.2]},
                    {"values": [0.3, 0.4]},
                ]
            }

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    result = await GeminiProvider().embed(["x", "y"], "text-embedding-004", api_key="g-key")
    assert result.embeddings == [[0.1, 0.2], [0.3, 0.4]]
    assert captured["url"].endswith("/models/text-embedding-004:batchEmbedContents")
    assert captured["headers"]["x-goog-api-key"] == "g-key"
    # Each input becomes a qualified request.
    reqs = captured["json"]["requests"]
    assert reqs[0]["model"] == "models/text-embedding-004"
    assert reqs[0]["content"]["parts"][0]["text"] == "x"
