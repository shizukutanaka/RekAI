"""`max_completion_tokens` must survive to providers under its own semantics.

OpenAI renamed `max_tokens` → `max_completion_tokens`, and o-series/gpt-5-class
models reject the old name outright. The compat layer used to collapse the two
(`req.max_tokens or req.max_completion_tokens`), so a caller sending the
required name produced an upstream payload with the rejected one. ChatRequest
now carries both; OpenAI-compatible providers forward each set field verbatim,
and providers without the distinction treat it as `max_tokens`.
"""

from __future__ import annotations

import httpx

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


async def test_max_completion_tokens_sent_under_its_own_name(monkeypatch) -> None:
    """The regression: the renamed field must reach OpenAI as itself, not as
    the name o-series rejects."""
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(max_completion_tokens=64), api_key="sk-x")
    assert _Client.captured["max_completion_tokens"] == 64
    assert "max_tokens" not in _Client.captured


async def test_both_spellings_forward_when_both_set(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(max_tokens=32, max_completion_tokens=64), api_key="sk-x")
    assert _Client.captured["max_tokens"] == 32
    assert _Client.captured["max_completion_tokens"] == 64


async def test_neither_set_sends_neither(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(), api_key="sk-x")
    assert "max_tokens" not in _Client.captured
    assert "max_completion_tokens" not in _Client.captured


async def test_anthropic_treats_it_as_max_tokens(monkeypatch) -> None:
    """Anthropic requires max_tokens; a caller's max_completion_tokens is the
    same cap in their vocabulary."""
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await AnthropicProvider().chat(_req(max_completion_tokens=64), api_key="sk-x")
    assert _Client.captured["max_tokens"] == 64


async def test_anthropic_prefers_max_tokens_when_both(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await AnthropicProvider().chat(_req(max_tokens=32, max_completion_tokens=64), api_key="sk-x")
    assert _Client.captured["max_tokens"] == 32


async def test_gemini_maps_to_max_output_tokens(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await GeminiProvider().chat(_req(max_completion_tokens=64), api_key="sk-x")
    assert _Client.captured["generationConfig"]["maxOutputTokens"] == 64


async def test_ollama_maps_to_num_predict(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OllamaProvider().chat(_req(max_completion_tokens=64), api_key=None)
    assert _Client.captured["options"]["num_predict"] == 64


def test_compat_layer_preserves_the_field() -> None:
    """ChatCompletionsRequest.max_completion_tokens must reach ChatRequest
    unchanged — the old `or` fold was the bug."""
    req = ChatCompletionsRequest(
        model="gpt-5",
        messages=[{"role": "user", "content": "hi"}],
        max_completion_tokens=64,
    )
    out = to_chat_request(req)
    assert out.max_completion_tokens == 64
    assert out.max_tokens is None


def test_the_two_spellings_key_differently() -> None:
    """max_tokens=64 and max_completion_tokens=64 produce different upstream
    payloads (different field names), so they must not share a cache entry."""
    assert cache_key(_req(max_tokens=64), "openai") != cache_key(
        _req(max_completion_tokens=64), "openai"
    )
    a = semantic_bucket(_req(max_tokens=64), "openai", "client-1")
    b = semantic_bucket(_req(max_completion_tokens=64), "openai", "client-1")
    assert a != b
