"""`service_tier` must reach OpenAI-compatible providers, and only them.

OpenAI's processing tiers (`flex` trades latency for a large discount,
`priority` pays for lower latency) are a real cost/latency lever. The compat
layer tolerated the field via ``extra="allow"`` and silently dropped it, so a
caller routing gpt-5 through RekAI paid default-tier prices regardless.
Anthropic, Gemini, and Ollama have no such field — it must not reach them.
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


async def test_service_tier_reaches_openai(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(service_tier="flex"), api_key="sk-x")
    assert _Client.captured["service_tier"] == "flex"


@pytest.mark.parametrize(
    ("provider", "api_key", "container"),
    [
        (AnthropicProvider, "sk-x", lambda p: p),
        (GeminiProvider, "sk-x", lambda p: p["generationConfig"]),
        (OllamaProvider, None, lambda p: p["options"]),
    ],
)
async def test_service_tier_not_sent_to_others(monkeypatch, provider, api_key, container) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await provider().chat(_req(service_tier="flex"), api_key=api_key)
    sent = container(_Client.captured)
    assert not {"service_tier", "serviceTier"} & set(sent)


async def test_no_service_tier_sends_no_key(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(), api_key="sk-x")
    assert "service_tier" not in _Client.captured


def test_service_tier_changes_the_cache_key() -> None:
    """flex and priority are billed differently — a cached flex answer must not
    answer a priority request's cost expectations either."""
    assert cache_key(_req(), "openai") != cache_key(_req(service_tier="flex"), "openai")


def test_service_tier_changes_the_semantic_bucket() -> None:
    a = semantic_bucket(_req(), "openai", "client-1")
    b = semantic_bucket(_req(service_tier="flex"), "openai", "client-1")
    assert a != b


def test_compat_maps_service_tier() -> None:
    req = ChatCompletionsRequest(
        model="gpt-5",
        messages=[{"role": "user", "content": "hi"}],
        service_tier="priority",
    )
    assert to_chat_request(req).service_tier == "priority"
