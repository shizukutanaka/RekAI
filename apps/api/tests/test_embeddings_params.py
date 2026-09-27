"""`dimensions`/`encoding_format` must reach the providers that support them.

OpenAI's text-embedding-3+ models are matryoshka-trained — `dimensions`
selects a smaller output vector (cheaper storage, faster similarity) and
`encoding_format` picks float vs base64 wire encoding. The schema tolerated
neither (``extra="ignore"``), so a caller asking for a 256-dim embedding
silently got the full-size one. Gemini maps the same idea to
``outputDimensionality``; Ollama and echo have no such knobs — the params
must not reach them.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from rekai.cache import embedding_cache_key
from rekai.providers.base import ProviderError
from rekai.providers.gemini import GeminiProvider
from rekai.providers.ollama import OllamaProvider
from rekai.providers.openai import OpenAIProvider


class _Resp:
    status_code = 200
    headers: dict = {}
    text = ""

    def json(self) -> dict:
        return {
            "model": "text-embedding-3-small",
            "data": [{"index": 0, "embedding": [0.1, 0.2]}],
            "embeddings": [{"values": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }


class _Client:
    captured: dict = {}

    def __init__(self, *a: object, **k: object) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a: object):
        return False

    async def aclose(self) -> None:
        return None

    async def post(self, url, json=None, headers=None, **kw):
        _Client.captured = json or {}
        return _Resp()


async def test_dimensions_reaches_openai(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().embed(["hi"], "text-embedding-3-small", "sk-x", dimensions=256)
    assert _Client.captured["dimensions"] == 256


async def test_encoding_format_reaches_openai(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().embed(["hi"], "text-embedding-3-small", "sk-x", encoding_format="base64")
    assert _Client.captured["encoding_format"] == "base64"


async def test_neither_sends_no_keys(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().embed(["hi"], "text-embedding-3-small", "sk-x")
    assert "dimensions" not in _Client.captured
    assert "encoding_format" not in _Client.captured


async def test_dimensions_maps_to_gemini_output_dimensionality(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await GeminiProvider().embed(["hi"], "gemini-embedding-001", "gk", dimensions=128)
    assert _Client.captured["requests"][0]["outputDimensionality"] == 128


async def test_encoding_format_not_sent_to_gemini_or_ollama(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await GeminiProvider().embed(["hi"], "gemini-embedding-001", "gk", encoding_format="base64")
    assert "encoding_format" not in _Client.captured["requests"][0]
    assert "encodingFormat" not in _Client.captured["requests"][0]
    await OllamaProvider().embed(["hi"], "m", None, encoding_format="base64")
    assert "encoding_format" not in _Client.captured


def test_dimensions_changes_the_embeddings_cache_key() -> None:
    """A 256-dim embedding must never answer a 1536-dim request — the inputs
    are identical, only the param differs."""
    a = embedding_cache_key("openai", "text-embedding-3-small", ["hi"], dimensions=256)
    b = embedding_cache_key("openai", "text-embedding-3-small", ["hi"], dimensions=1536)
    c = embedding_cache_key("openai", "text-embedding-3-small", ["hi"])
    assert len({a, b, c}) == 3


def test_dimensions_validated_at_schema(client: TestClient) -> None:
    resp = client.post("/v1/embeddings", json={"model": "echo", "input": "hi", "dimensions": 0})
    assert resp.status_code == 422
    resp = client.post("/v1/embeddings", json={"model": "echo", "input": "hi", "dimensions": 8})
    assert resp.status_code == 200


async def test_token_array_input_reaches_openai_verbatim(monkeypatch) -> None:
    """Pre-tokenized input (OpenAI's list[int] / list[list[int]] form) forwards
    to the provider unchanged — token arrays are a first-class input there."""
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    result = await OpenAIProvider().embed([[1, 2, 3]], "text-embedding-3-small", "sk-x")
    assert _Client.captured["input"] == [[1, 2, 3]]
    assert result.embeddings


async def test_token_array_rejected_by_text_only_providers() -> None:
    """Gemini/Ollama have no token-array form — reject honestly rather than
    mangling ids into a text field."""
    for provider, model, key in (
        (GeminiProvider(), "gemini-embedding-001", "gk"),
        (OllamaProvider(), "m", None),
    ):
        with pytest.raises(ProviderError) as ei:
            await provider.embed([[1, 2, 3]], model, key)
        assert ei.value.status_code == 400


def test_token_array_input_accepted_and_embedded(client: TestClient) -> None:
    resp = client.post("/v1/embeddings", json={"model": "echo", "input": [1, 2, 3]})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["embeddings"]) == 1
    assert body["usage"]["prompt_tokens"] == 3


def test_token_array_batch_accepted(client: TestClient) -> None:
    resp = client.post("/v1/embeddings", json={"model": "echo", "input": [[1, 2], [3]]})
    assert resp.status_code == 200
    assert len(resp.json()["embeddings"]) == 2


def test_mixed_text_and_tokens_rejected_at_schema(client: TestClient) -> None:
    resp = client.post("/v1/embeddings", json={"model": "echo", "input": ["hi", [1]]})
    assert resp.status_code == 422


def test_empty_token_array_input_rejected(client: TestClient) -> None:
    assert client.post("/v1/embeddings", json={"model": "echo", "input": [[]]}).status_code == 422
