"""`reasoning_effort` must reach OpenAI-compatible providers, and only them.

OpenAI's reasoning models (o-series, gpt-5) take `reasoning_effort` to trade
latency/cost for answer depth. The compat layer tolerated it via
``extra="allow"`` and silently dropped it, so a caller routing o3 through
RekAI had no way to dial it. Anthropic, Gemini, and Ollama have no such field
— it must not be forwarded to them.
"""

from __future__ import annotations

import httpx
import pytest

from rekai.cache import cache_key, semantic_bucket
from rekai.openai_compat import to_chat_request
from rekai.providers.anthropic import AnthropicProvider
from rekai.providers.gemini import GeminiProvider
from rekai.providers.ollama import OllamaProvider
from rekai.providers.openai import OpenAIProvider
from rekai.schemas import ChatCompletionsRequest, ChatMessage, ChatRequest


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


async def test_reasoning_effort_reaches_openai(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(reasoning_effort="high"), api_key="sk-x")
    assert _Client.captured["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    ("provider", "api_key", "container"),
    [
        (AnthropicProvider, "sk-x", lambda p: p),
        (GeminiProvider, "sk-x", lambda p: p["generationConfig"]),
        (OllamaProvider, None, lambda p: p["options"]),
    ],
)
async def test_reasoning_effort_not_sent_to_others(
    monkeypatch, provider, api_key, container
) -> None:
    """Anthropic/Gemini/Ollama have no reasoning_effort field."""
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await provider().chat(_req(reasoning_effort="high"), api_key=api_key)
    sent = container(_Client.captured)
    assert not {"reasoning_effort", "reasoningEffort"} & set(sent)


async def test_no_reasoning_effort_sends_no_key(monkeypatch) -> None:
    """Absent means absent — a default effort is not the same request."""
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(), api_key="sk-x")
    assert "reasoning_effort" not in _Client.captured


def test_reasoning_effort_changes_the_cache_key() -> None:
    """effort=high and effort=low produce different answers — same prompt,
    different depth."""
    assert cache_key(_req(), "openai") != cache_key(_req(reasoning_effort="high"), "openai")


def test_reasoning_effort_changes_the_semantic_bucket() -> None:
    a = semantic_bucket(_req(), "openai", "client-1")
    b = semantic_bucket(_req(reasoning_effort="high"), "openai", "client-1")
    assert a != b


def test_compat_maps_reasoning_effort() -> None:
    req = ChatCompletionsRequest(
        model="o3",
        messages=[{"role": "user", "content": "hi"}],
        reasoning_effort="low",
    )
    assert to_chat_request(req).reasoning_effort == "low"
