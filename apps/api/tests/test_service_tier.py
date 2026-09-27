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


# --- prompt_cache_key / prompt_cache_retention -----------------------------


def test_prompt_cache_fields_map_through_compat() -> None:
    req = ChatCompletionsRequest.model_validate(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "prompt_cache_key": "tenant-7",
            "prompt_cache_retention": "24h",
        }
    )
    chat = to_chat_request(req)
    assert chat.prompt_cache_key == "tenant-7"
    assert chat.prompt_cache_retention == "24h"


def test_prompt_cache_fields_absent_by_default() -> None:
    req = ChatCompletionsRequest.model_validate(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    )
    chat = to_chat_request(req)
    assert chat.prompt_cache_key is None
    assert chat.prompt_cache_retention is None


def test_prompt_cache_fields_not_in_cache_key() -> None:
    """Routing hints don't change the response — two requests differing only
    in cache affinity must share RekAI's cache entry."""
    a = _req(prompt_cache_key="tenant-a", prompt_cache_retention="24h")
    b = _req(prompt_cache_key="tenant-b", prompt_cache_retention="in-memory")
    assert cache_key(a, "openai") == cache_key(b, "openai")
    assert semantic_bucket(a, "openai", "c") == semantic_bucket(b, "openai", "c")


@pytest.mark.asyncio
async def test_prompt_cache_fields_reach_openai(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(
        _req(prompt_cache_key="tenant-7", prompt_cache_retention="24h"),
        api_key="sk-x",
    )
    assert _Client.captured["prompt_cache_key"] == "tenant-7"
    assert _Client.captured["prompt_cache_retention"] == "24h"


@pytest.mark.asyncio
async def test_prompt_cache_fields_not_sent_to_others(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    req = _req(prompt_cache_key="tenant-7", prompt_cache_retention="24h")
    for provider in (AnthropicProvider, GeminiProvider, OllamaProvider):
        await provider().chat(req, api_key="sk-x")
        assert "prompt_cache_key" not in _Client.captured
        assert "prompt_cache_retention" not in _Client.captured
