"""`output_config` (effort/format) must reach Anthropic, and only it.

`output_config` is a real top-level Messages API field: `effort` controls how
many tokens Claude spends and `format` requests structured output. The compat
layer tolerated it via ``extra="allow"`` and silently dropped it, so an SDK
caller asking for ``effort: "medium"`` got a default-effort answer — with no
error to flag the loss. OpenAI, Gemini, and Ollama have no such field — it must
not reach them.
"""

from __future__ import annotations

import httpx
import pytest

from rekai.anthropic_compat import to_chat_request
from rekai.cache import cache_key, semantic_bucket
from rekai.providers.anthropic import AnthropicProvider
from rekai.providers.gemini import GeminiProvider
from rekai.providers.ollama import OllamaProvider
from rekai.providers.openai import OpenAIProvider
from rekai.schemas import AnthropicMessagesRequest, ChatMessage, ChatRequest


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


async def test_output_config_reaches_anthropic(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await AnthropicProvider().chat(_req(output_config={"effort": "medium"}), api_key="sk-x")
    assert _Client.captured["output_config"] == {"effort": "medium"}


@pytest.mark.parametrize(
    ("provider", "api_key", "container"),
    [
        (OpenAIProvider, "sk-x", lambda p: p),
        (GeminiProvider, "sk-x", lambda p: p["generationConfig"]),
        (OllamaProvider, None, lambda p: p["options"]),
    ],
)
async def test_output_config_not_sent_to_others(monkeypatch, provider, api_key, container) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await provider().chat(_req(output_config={"effort": "medium"}), api_key=api_key)
    sent = container(_Client.captured)
    assert not {"output_config", "outputConfig"} & set(sent)


async def test_no_output_config_sends_no_key(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await AnthropicProvider().chat(_req(), api_key="sk-x")
    assert "output_config" not in _Client.captured


def test_output_config_changes_the_cache_key() -> None:
    """Different effort levels shape both the content and the token bill — a
    cached answer must not serve a request that asked for different effort."""
    assert cache_key(_req(), "anthropic") != cache_key(
        _req(output_config={"effort": "medium"}), "anthropic"
    )


def test_output_config_changes_the_semantic_bucket() -> None:
    a = semantic_bucket(_req(), "anthropic", "client-1")
    b = semantic_bucket(_req(output_config={"effort": "medium"}), "anthropic", "client-1")
    assert a != b


def test_compat_maps_output_config() -> None:
    req = AnthropicMessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 2000,
            "output_config": {"effort": "medium"},
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert to_chat_request(req).output_config == {"effort": "medium"}
