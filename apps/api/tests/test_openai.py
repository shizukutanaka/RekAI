"""Tests for the OpenAI provider's error paths and SSE parsers.

The happy paths live across the feature test files (tuning params, streaming,
embeddings params, moderations); these cover what those don't reach — the
branches a client sees when the upstream errors or the stream answer is
corrupt, plus the SSE-line skip rules that keep a keep-alive or truncated
chunk from aborting a request.
"""

from __future__ import annotations

import httpx
import pytest

from rekai.providers.base import ProviderError
from rekai.providers.openai import (
    OpenAIProvider,
    _accumulate_tool_call_deltas,
    _parse_openai_sse_event,
)
from rekai.schemas import ChatMessage, ChatRequest


def _req(**kwargs) -> ChatRequest:
    kwargs.setdefault("model", "gpt-4o-mini")
    kwargs.setdefault("messages", [ChatMessage(role="user", content="hi")])
    return ChatRequest(**kwargs)


class _ErrorResponse:
    """The fields provider_http_error reads: status_code, text, headers."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = "upstream said no"
        self.headers: dict = {}


class _ErrClient:
    """POST answers with a 500."""

    def __init__(self, *a, **k) -> None:
        pass

    async def post(self, *a, **k):
        return _ErrorResponse(500)


class _DownClient:
    """POST fails at the transport layer (DNS / refused / TLS)."""

    def __init__(self, *a, **k) -> None:
        pass

    async def post(self, *a, **k):
        raise httpx.ConnectError("connection refused")


async def test_chat_forwards_max_tokens(monkeypatch) -> None:
    class _Resp:
        status_code = 200

        def json(self) -> dict:
            return {
                "model": "gpt-4o-mini",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    class _Client:
        captured: dict = {}

        def __init__(self, *a, **k) -> None:
            pass

        async def post(self, url, json, headers):
            _Client.captured = {"json": json}
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(max_tokens=32), api_key="sk-x")
    assert _Client.captured["json"]["max_tokens"] == 32


async def test_chat_upstream_http_error_is_mapped(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _ErrClient)
    with pytest.raises(ProviderError) as exc:
        await OpenAIProvider().chat(_req(), api_key="sk-x")
    assert exc.value.status_code == 502  # upstream 5xx normalised to bad gateway


async def test_chat_transport_failure_is_wrapped(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _DownClient)
    with pytest.raises(ProviderError):
        await OpenAIProvider().chat(_req(), api_key="sk-x")


async def test_stream_upstream_error_status_propagates(monkeypatch) -> None:
    from tests.test_streaming import _FakeClient

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient([], status_code=429))
    with pytest.raises(ProviderError) as exc:
        async for _ in OpenAIProvider().stream_events(_req(), api_key="sk-x"):
            pass
    assert exc.value.status_code == 429


async def test_stream_transport_failure_is_wrapped(monkeypatch) -> None:
    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        def stream(self, *a, **k):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    with pytest.raises(ProviderError):
        async for _ in OpenAIProvider().stream_events(_req(), api_key="sk-x"):
            pass


async def test_embed_upstream_http_error_is_mapped(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _ErrClient)
    with pytest.raises(ProviderError) as exc:
        await OpenAIProvider().embed(["hi"], model="text-embedding-3-small", api_key="sk-x")
    assert exc.value.status_code == 502


async def test_embed_transport_failure_is_wrapped(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _DownClient)
    with pytest.raises(ProviderError):
        await OpenAIProvider().embed(["hi"], model="text-embedding-3-small", api_key="sk-x")


async def test_moderation_upstream_http_error_is_mapped(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _ErrClient)
    with pytest.raises(ProviderError) as exc:
        await OpenAIProvider().moderate("hi", model="omni-moderation-latest", api_key="sk-x")
    assert exc.value.status_code == 502


async def test_moderation_transport_failure_is_wrapped(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _DownClient)
    with pytest.raises(ProviderError):
        await OpenAIProvider().moderate("hi", model="omni-moderation-latest", api_key="sk-x")


def test_parse_sse_event_skips_done_and_malformed_lines() -> None:
    assert _parse_openai_sse_event("data: [DONE]") is None
    assert _parse_openai_sse_event("data:") is None
    assert _parse_openai_sse_event(": keep-alive") is None
    assert _parse_openai_sse_event('data: {"choices":[{"delta":{"content":"cut o') is None


def test_accumulate_tool_call_deltas_skips_done_and_malformed_lines() -> None:
    acc: dict = {}
    _accumulate_tool_call_deltas("data: [DONE]", acc)
    _accumulate_tool_call_deltas("data:", acc)
    _accumulate_tool_call_deltas(": keep-alive", acc)
    _accumulate_tool_call_deltas('data: {"choices":[{"delta":{"tool_calls":[{', acc)
    assert acc == {}
