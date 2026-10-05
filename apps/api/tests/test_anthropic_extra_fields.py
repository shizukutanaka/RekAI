"""Remaining real Anthropic request fields ride verbatim — Anthropic only.

`context_management` (context editing), `container` (code-execution reuse),
`inference_geo` (data residency), `speed` (fast/standard pricing), `diagnostics`
(prompt-cache divergence reporting), and `user_profile_id` all rode in under
``extra="allow"`` on the compat surface and were silently dropped at the
ChatRequest boundary — a caller setting context-editing rules got a plain
request with no error. Each is now declared, mapped, sent verbatim upstream,
and part of the cache keys. OpenAI, Gemini, and Ollama have no such fields —
they must never receive them.

Anthropic's own `fallbacks` is deliberately *not* forwarded: RekAI's extension
uses the same name with different semantics (provider fallback targets vs
Anthropic's model fallbacks), so forwarding would misroute.
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
    headers: dict = {}

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
        _Client.headers = headers or {}
        return _Resp()


_FIELDS = {
    "context_management": {
        "edits": [{"type": "clear_tool_uses_20250919", "keep": {"type": "tool_uses", "value": 1}}]
    },
    "container": {"id": "container_abc"},
    "inference_geo": "us",
    "speed": "fast",
    "diagnostics": {"previous_response_id": "msg_123"},
    "user_profile_id": "profile_abc",
}


_BODY_FIELDS = {k: v for k, v in _FIELDS.items() if k != "user_profile_id"}


@pytest.mark.parametrize(("field", "value"), _BODY_FIELDS.items())
async def test_field_reaches_anthropic(monkeypatch, field: str, value: object) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await AnthropicProvider().chat(_req(**{field: value}), api_key="sk-x")
    assert _Client.captured[field] == value


async def test_user_profile_id_is_sent_as_header(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await AnthropicProvider().chat(_req(user_profile_id="profile_abc"), api_key="sk-x")
    assert _Client.headers["anthropic-user-profile-id"] == "profile_abc"
    assert "user_profile_id" not in _Client.captured


async def test_container_id_string_reaches_anthropic(monkeypatch) -> None:
    # Container reuse passes the bare id string returned by a prior response.
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await AnthropicProvider().chat(_req(container="container_abc"), api_key="sk-x")
    assert _Client.captured["container"] == "container_abc"


@pytest.mark.parametrize("field", _FIELDS)
@pytest.mark.parametrize(
    ("provider", "api_key", "container"),
    [
        (OpenAIProvider, "sk-x", lambda p: p),
        (GeminiProvider, "sk-x", lambda p: p["generationConfig"]),
        (OllamaProvider, None, lambda p: p["options"]),
    ],
)
async def test_field_not_sent_to_others(
    monkeypatch, provider, api_key, container, field: str
) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await provider().chat(_req(**{field: _FIELDS[field]}), api_key=api_key)
    assert field not in container(_Client.captured)


async def test_unset_fields_send_no_keys(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await AnthropicProvider().chat(_req(), api_key="sk-x")
    assert not set(_FIELDS) & set(_Client.captured)


@pytest.mark.parametrize("field", _FIELDS)
def test_field_changes_the_cache_key(field: str) -> None:
    assert cache_key(_req(), "anthropic") != cache_key(_req(**{field: _FIELDS[field]}), "anthropic")


@pytest.mark.parametrize("field", _FIELDS)
def test_field_changes_the_semantic_bucket(field: str) -> None:
    a = semantic_bucket(_req(), "anthropic", "client-1")
    b = semantic_bucket(_req(**{field: _FIELDS[field]}), "anthropic", "client-1")
    assert a != b


@pytest.mark.parametrize(("field", "value"), _FIELDS.items())
def test_compat_maps_field(field: str, value: object) -> None:
    req = AnthropicMessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 2000,
            field: value,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert getattr(to_chat_request(req), field) == value


def test_anthropic_fallbacks_is_not_misread_as_rekai_fallbacks() -> None:
    """Anthropic's `fallbacks` is a list of model names; RekAI's is provider
    fallback targets. A client sending Anthropic-shaped fallbacks must not get
    it silently validated into RekAI's routing extension."""
    req = AnthropicMessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 2000,
            "fallbacks": ["claude-haiku-4-5"],
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    chat_req = to_chat_request(req)
    assert chat_req.fallbacks is None


def test_anthropic_cache_extension_reaches_chat_request() -> None:
    """`cache` is RekAI's own extension on this surface (Anthropic has no such
    field); extra="allow" used to swallow "cache": false silently."""
    req = AnthropicMessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 2000,
            "cache": False,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert to_chat_request(req).cache is False
