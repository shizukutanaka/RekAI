"""`top_k` must reach the providers that support it, and must key the cache.

Anthropic, Gemini, and Ollama all take a top-k bound; OpenAI's chat API does
not, so it must *not* be forwarded there. Before this, a caller had no way to
narrow the sampling pool on the three backends that offer it — a gap LiteLLM
and Portkey both close.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rekai.cache import cache_key, semantic_bucket
from rekai.config import Settings
from rekai.main import create_app
from rekai.providers.anthropic import AnthropicProvider
from rekai.providers.gemini import GeminiProvider
from rekai.providers.ollama import OllamaProvider
from rekai.providers.openai import OpenAIProvider
from rekai.schemas import ChatMessage, ChatRequest


def _req(**kw: object) -> ChatRequest:
    kw.setdefault("model", "m")
    kw.setdefault("messages", [ChatMessage(role="user", content="write an essay")])
    return ChatRequest(**kw)  # type: ignore[arg-type]


class _Resp:
    status_code = 200
    headers: dict = {}
    text = ""

    def json(self) -> dict:
        return {
            "model": "m",
            "message": {"content": "hi"},
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
            "content": [{"type": "text", "text": "hi"}],
            "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
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


# --- reaches each backend under its own name ---------------------------------


@pytest.mark.parametrize(
    ("provider", "api_key", "locate"),
    [
        (AnthropicProvider, "sk-x", lambda p: p.get("top_k")),
        (GeminiProvider, "sk-x", lambda p: p["generationConfig"].get("topK")),
        (OllamaProvider, None, lambda p: p["options"].get("top_k")),
    ],
)
async def test_top_k_reaches_the_provider(monkeypatch, provider, api_key, locate) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await provider().chat(_req(top_k=40), api_key=api_key)
    assert locate(_Client.captured) == 40


async def test_top_k_is_not_sent_to_openai(monkeypatch) -> None:
    """OpenAI's chat API has no top_k — forwarding it would 400."""
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(top_k=40), api_key="sk-x")
    assert "top_k" not in _Client.captured


@pytest.mark.parametrize(
    ("provider", "api_key", "container"),
    [
        (AnthropicProvider, "sk-x", lambda p: p),
        (GeminiProvider, "sk-x", lambda p: p["generationConfig"]),
        (OllamaProvider, None, lambda p: p["options"]),
    ],
)
async def test_no_top_k_sends_no_key(monkeypatch, provider, api_key, container) -> None:
    """Absent means absent — a default top_k is not the same request as
    `top_k=40`."""
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await provider().chat(_req(), api_key=api_key)
    sent = container(_Client.captured)
    assert not {"top_k", "topK"} & set(sent)


# --- validation ---------------------------------------------------------------


@pytest.mark.parametrize("value", [0, -1])
def test_top_k_must_be_positive(value: int) -> None:
    """top_k=0 would sample from an empty pool."""
    with pytest.raises(ValidationError):
        _req(top_k=value)


# --- the cache must not collide -----------------------------------------------


def test_top_k_changes_the_cache_key() -> None:
    """Two requests alike but for `top_k` get different answers."""
    assert cache_key(_req(), "openai") != cache_key(_req(top_k=40), "openai")
    assert cache_key(_req(top_k=10), "openai") != cache_key(_req(top_k=40), "openai")


def test_top_k_changes_the_semantic_bucket() -> None:
    a = semantic_bucket(_req(), "openai", "client-1")
    b = semantic_bucket(_req(top_k=40), "openai", "client-1")
    assert a != b


# --- compat surface -----------------------------------------------------------


def test_top_k_on_the_compat_route(monkeypatch) -> None:
    """`top_k` is a RekAI extension on /v1/chat/completions — it must reach
    the provider, not be dropped by `extra='allow'`."""
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    app = create_app(Settings(environment="test", default_provider="ollama"))
    resp = TestClient(app).post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "top_k": 40,
        },
    )
    assert resp.status_code == 200
    assert _Client.captured["options"]["top_k"] == 40
