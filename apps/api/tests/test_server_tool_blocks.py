"""Anthropic server-side tool blocks — response blocks ride through verbatim.

`web_search`, MCP connectors and code execution make Anthropic emit blocks
RekAI doesn't map (``server_tool_use``, ``web_search_tool_result``,
``mcp_tool_use``/``mcp_tool_result``, ``code_execution_tool_result``, ...).
They used to be dropped silently: the answer came back without its tool-trace,
and echoing that history next turn 400'd. Both directions now pass verbatim.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from rekai import anthropic_compat
from rekai.anthropic_compat import to_message
from rekai.cache import NullCache
from rekai.config import Settings
from rekai.providers import register_provider
from rekai.providers.anthropic import AnthropicProvider
from rekai.providers.base import Provider, ProviderError, ProviderResult, StreamEvent
from rekai.schemas import AnthropicMessagesRequest, ChatMessage, ChatRequest, Usage
from rekai.service import handle_chat, handle_chat_stream

_SERVER_USE = {
    "type": "server_tool_use",
    "id": "srvtoolu_1",
    "name": "web_search",
    "input": {"query": "latest news"},
}
_SEARCH_RESULT = {
    "type": "web_search_tool_result",
    "tool_use_id": "srvtoolu_1",
    "content": [
        {
            "type": "web_search_result",
            "url": "https://example.com/a",
            "title": "Article",
            "encrypted_content": "enc…",
        }
    ],
}


def _req() -> ChatRequest:
    return ChatRequest(model="claude-sonnet-4-6", messages=[ChatMessage(role="user", content="hi")])


def _resp(blocks: list[dict]) -> dict:
    return {
        "model": "claude-sonnet-4-6",
        "content": blocks,
        "usage": {"input_tokens": 5, "output_tokens": 9},
    }


class _FakeResp:
    status_code = 200

    def __init__(self, data: dict) -> None:
        self._data = data

    def json(self) -> dict:
        return self._data


class _FakeClient:
    """Captures the outgoing payload and replays a canned non-stream reply."""

    captured: dict = {}
    data: dict = {}

    def __init__(self, *a, **k) -> None:
        pass

    async def aclose(self) -> None:
        return None

    async def post(self, url, json=None, headers=None):
        _FakeClient.captured = json
        return _FakeResp(_FakeClient.data)


class _StreamResp:
    status_code = 200

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _StreamCtx:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def __aenter__(self):
        return _StreamResp(self._lines)

    async def __aexit__(self, *exc):
        return None


def _stream_client(lines: list[str]):
    class _C:
        def __init__(self, *a, **k) -> None:
            pass

        def stream(self, *a, **k):
            return _StreamCtx(lines)

    return _C


def _sse(event_type: str, data: dict) -> str:
    return f"data: {json.dumps({'type': event_type, **data})}"


# --- non-stream --------------------------------------------------------------


def test_provider_keeps_server_tool_blocks_verbatim(monkeypatch) -> None:
    _FakeClient.data = _resp([_SERVER_USE, _SEARCH_RESULT, {"type": "text", "text": "answer"}])
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    provider = AnthropicProvider()
    result = asyncio.run(provider.chat(_req(), "k"))
    assert result.content == "answer"
    assert result.extra_blocks == [_SERVER_USE, _SEARCH_RESULT]


def test_provider_no_extra_blocks_returns_none(monkeypatch) -> None:
    _FakeClient.data = _resp([{"type": "text", "text": "answer"}])
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(AnthropicProvider().chat(_req(), "k"))
    assert result.extra_blocks is None


# --- provider stream ---------------------------------------------------------


async def _collect(lines: list[str]) -> list[StreamEvent]:
    monkey_client = _stream_client(lines)
    import unittest.mock as mock

    with mock.patch.object(httpx, "AsyncClient", monkey_client):
        provider = AnthropicProvider()
        return [e async for e in provider.stream_events(_req(), "k")]


async def test_stream_server_tool_use_round_trip(monkeypatch) -> None:
    lines = [
        _sse("content_block_start", {"index": 0, "content_block": dict(_SERVER_USE, input={})}),
        _sse(
            "content_block_delta",
            {"index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"query": "la'}},
        ),
        _sse(
            "content_block_delta",
            {"index": 0, "delta": {"type": "input_json_delta", "partial_json": 'test news"}'}},
        ),
        _sse("content_block_stop", {"index": 0}),
        _sse(
            "content_block_start",
            {"index": 1, "content_block": {"type": "text", "text": ""}},
        ),
        _sse(
            "content_block_delta",
            {"index": 1, "delta": {"type": "text_delta", "text": "answer"}},
        ),
        _sse("content_block_stop", {"index": 1}),
        _sse("message_delta", {"delta": {"stop_reason": "end_turn"}}),
    ]
    events = await _collect(lines)
    starts = [e.extra_block_start for e in events if e.extra_block_start]
    deltas = [e.extra_block_delta for e in events if e.extra_block_delta]
    done = [e.extra_block for e in events if e.extra_block]
    assert starts == [dict(_SERVER_USE, input={})]
    assert len(deltas) == 2
    assert done[0]["type"] == "server_tool_use"
    assert done[0]["input"] == {"query": "latest news"}


async def test_stream_tool_result_block_arrives_whole(monkeypatch) -> None:
    lines = [
        _sse("content_block_start", {"index": 0, "content_block": _SEARCH_RESULT}),
        _sse("content_block_stop", {"index": 0}),
        _sse("message_delta", {"delta": {"stop_reason": "end_turn"}}),
    ]
    events = await _collect(lines)
    done = [e.extra_block for e in events if e.extra_block]
    assert done == [_SEARCH_RESULT]


# --- compat ------------------------------------------------------------------


def test_to_message_inserts_extra_blocks_before_text() -> None:
    from rekai.schemas import ChatResponse

    resp = ChatResponse(
        id="r1",
        provider="anthropic",
        model="m",
        content="answer",
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        thinking_blocks=[{"type": "thinking", "thinking": "t", "signature": "s"}],
        extra_blocks=[_SERVER_USE, _SEARCH_RESULT],
        created=0,
    )
    types = [b["type"] for b in to_message(resp)["content"]]
    assert types == ["thinking", "server_tool_use", "web_search_tool_result", "text"]


def test_history_echo_preserves_server_blocks() -> None:
    """Multi-turn: an assistant turn carrying the tool-trace echoes verbatim —
    Anthropic requires it for context."""
    req = AnthropicMessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 2000,
            "messages": [
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "t", "signature": "s"},
                        _SERVER_USE,
                        _SEARCH_RESULT,
                        {"type": "text", "text": "the answer"},
                    ],
                },
                {"role": "user", "content": "and then?"},
            ],
        }
    )
    chat = anthropic_compat.to_chat_request(req)
    assistant = chat.messages[1]
    assert assistant.extra_blocks == [_SERVER_USE, _SEARCH_RESULT]

    payload = AnthropicProvider()._build_payload(chat, stream=False)
    types = [b["type"] for b in payload["messages"][1]["content"]]
    assert types == ["thinking", "server_tool_use", "web_search_tool_result", "text"]
    assert payload["messages"][1]["content"][1] == _SERVER_USE


def test_user_turn_server_block_is_400() -> None:
    req = AnthropicMessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": [_SERVER_USE]}],
        }
    )
    with pytest.raises(ProviderError):
        anthropic_compat.to_chat_request(req)


# --- service pass-through ----------------------------------------------------


class _ServerToolProvider(Provider):
    name = "svc-servertool"
    requires_key = False

    async def chat(self, request, api_key):
        return ProviderResult(
            model="x",
            content="answer",
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            extra_blocks=[_SERVER_USE, _SEARCH_RESULT],
        )

    async def stream_events(self, request, api_key):
        yield StreamEvent(extra_block_start=dict(_SERVER_USE, input={}))
        yield StreamEvent(extra_block_delta={"type": "input_json_delta", "partial_json": "{}"})
        yield StreamEvent(extra_block=_SERVER_USE)
        yield StreamEvent(delta="answer")
        yield StreamEvent(
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            finish_reason="stop",
        )


async def test_service_carries_extra_blocks() -> None:
    provider = _ServerToolProvider()
    register_provider(provider)
    result = await handle_chat(
        _req().model_copy(update={"model": "x", "provider": "svc-servertool"}),
        None,
        Settings(environment="test", default_provider="echo"),
        NullCache(),
    )
    assert result.extra_blocks == [_SERVER_USE, _SEARCH_RESULT]


async def test_service_streams_extra_events() -> None:
    provider = _ServerToolProvider()
    register_provider(provider)
    events = [
        e
        async for e in handle_chat_stream(
            ChatRequest(
                model="x",
                provider="svc-servertool",
                messages=[ChatMessage(role="user", content="hi")],
            ),
            None,
            Settings(environment="test", default_provider="echo"),
            NullCache(),
            "svc-servertool",
            provider,
            "client-a",
        )
    ]
    assert any(e.extra_block_start for e in events)
    assert any(e.extra_block_delta for e in events)
    assert any(e.extra_block == _SERVER_USE for e in events)


# --- /v1/messages stream ------------------------------------------------------


def test_messages_stream_emits_verbatim_block_frames(client: TestClient, monkeypatch) -> None:
    """Server-tool blocks stream as their own content blocks, start→delta→stop."""

    async def fake_stream_events(self, request, api_key):
        yield StreamEvent(extra_block_start=dict(_SERVER_USE, input={}))
        yield StreamEvent(
            extra_block_delta={"type": "input_json_delta", "partial_json": '{"query":"x"}'}
        )
        yield StreamEvent(extra_block=dict(_SERVER_USE, input={"query": "x"}))
        yield StreamEvent(delta="answer")
        yield StreamEvent(finish_reason="stop")

    monkeypatch.setattr(AnthropicProvider, "stream_events", fake_stream_events)
    resp = client.post(
        "/v1/messages",
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
        headers={"X-Provider-Key": "k"},
    )
    assert resp.status_code == 200
    events = []
    current = None
    for line in resp.text.splitlines():
        if line.startswith("event:"):
            current = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and current:
            events.append((current, json.loads(line.split(":", 1)[1])))
            current = None
    starts = [d for n, d in events if n == "content_block_start"]
    assert starts[0]["content_block"]["type"] == "server_tool_use"
    assert starts[0]["content_block"]["name"] == "web_search"
    block_delta = next(
        d
        for n, d in events
        if n == "content_block_delta" and d["delta"]["type"] == "input_json_delta"
    )
    assert block_delta["delta"]["partial_json"] == '{"query":"x"}'
    # text block follows the server block at the next index
    text_start = next(d for n, d in events if n == "content_block_start" and d["index"] == 1)
    assert text_start["content_block"]["type"] == "text"


def test_native_stream_emits_extra_block_frame(client: TestClient, monkeypatch) -> None:
    async def fake_stream_events(self, request, api_key):
        yield StreamEvent(extra_block=_SERVER_USE)
        yield StreamEvent(delta="answer")
        yield StreamEvent(finish_reason="stop")

    from rekai.providers.echo import EchoProvider

    monkeypatch.setattr(EchoProvider, "stream_events", fake_stream_events)
    resp = client.post(
        "/v1/chat/stream",
        json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    frames = [
        json.loads(line.split(":", 1)[1])
        for line in resp.text.splitlines()
        if line.startswith("data:") and "[DONE]" not in line
    ]
    assert any("extra_block" in f and f["extra_block"]["name"] == "web_search" for f in frames)


# --- message-level extras (container, context_management, ...) ----------------


_CONTAINER = {"id": "container_abc", "expires_at": "2026-01-01T00:00:00Z"}


def _resp_with_extras() -> dict:
    data = _resp([{"type": "text", "text": "answer"}])
    data["container"] = dict(_CONTAINER)
    data["context_management"] = {"applied_edits": [{"type": "clear_thinking_20251013"}]}
    return data


def test_provider_keeps_message_level_extras_verbatim(monkeypatch) -> None:
    """container/context_management ride extra_fields, forward-compatible."""
    _FakeClient.data = _resp_with_extras()
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(AnthropicProvider().chat(_req(), "k"))
    assert result.extra_fields == {
        "container": dict(_CONTAINER),
        "context_management": {"applied_edits": [{"type": "clear_thinking_20251013"}]},
    }


def test_provider_no_extras_returns_none(monkeypatch) -> None:
    _FakeClient.data = _resp([{"type": "text", "text": "answer"}])
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    result = asyncio.run(AnthropicProvider().chat(_req(), "k"))
    assert result.extra_fields is None


def test_to_message_reattaches_extras_without_clobbering() -> None:
    from rekai.schemas import ChatResponse

    resp = ChatResponse(
        id="x",
        provider="anthropic",
        model="claude-sonnet-4-6",
        content="answer",
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        created=0,
        extra_fields={"container": dict(_CONTAINER), "model": "spoofed"},
    )
    msg = to_message(resp)
    assert msg["container"] == _CONTAINER
    # setdefault: extras can never clobber gateway-computed fields
    assert msg["model"] == "claude-sonnet-4-6"


async def test_stream_message_start_extras(monkeypatch) -> None:
    lines = [
        _sse(
            "message_start",
            {
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "content": [],
                    "container": dict(_CONTAINER),
                    "usage": {"input_tokens": 5},
                }
            },
        ),
        _sse(
            "content_block_start",
            {"index": 0, "content_block": {"type": "text", "text": ""}},
        ),
        _sse("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "hi"}}),
        _sse("content_block_stop", {"index": 0}),
        _sse(
            "message_delta",
            {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        ),
        _sse("message_stop", {}),
    ]
    events = await _collect(lines)
    extras = next(e for e in events if e.extra_fields is not None)
    assert extras.extra_fields == {"container": dict(_CONTAINER)}


def test_messages_stream_merges_extras_into_message_start(client: TestClient, monkeypatch) -> None:
    async def fake_stream_events(self, request, api_key):
        yield StreamEvent(extra_fields={"container": dict(_CONTAINER)})
        yield StreamEvent(delta="answer")
        yield StreamEvent(finish_reason="stop")

    from rekai.providers.echo import EchoProvider

    monkeypatch.setattr(EchoProvider, "stream_events", fake_stream_events)
    resp = client.post(
        "/v1/messages",
        json={
            "model": "echo",
            "max_tokens": 10,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200
    events = []
    for line in resp.text.splitlines():
        if line.startswith("data:") and "[DONE]" not in line:
            events.append(json.loads(line.split(":", 1)[1]))
    start = next(e for e in events if e["type"] == "message_start")
    assert start["message"]["container"] == _CONTAINER


def test_native_stream_emits_extra_fields_frame(client: TestClient, monkeypatch) -> None:
    async def fake_stream_events(self, request, api_key):
        yield StreamEvent(extra_fields={"container": dict(_CONTAINER)})
        yield StreamEvent(delta="answer")
        yield StreamEvent(finish_reason="stop")

    from rekai.providers.echo import EchoProvider

    monkeypatch.setattr(EchoProvider, "stream_events", fake_stream_events)
    resp = client.post(
        "/v1/chat/stream",
        json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    frames = [
        json.loads(line.split(":", 1)[1])
        for line in resp.text.splitlines()
        if line.startswith("data:") and "[DONE]" not in line
    ]
    assert any(f.get("extra_fields", {}).get("container") == _CONTAINER for f in frames)


# --- review follow-ups --------------------------------------------------------


async def test_stream_extra_block_text_delta_stays_block_scoped(monkeypatch) -> None:
    """A text_delta inside a tracked extra block is not swallowed as answer text."""
    lines = [
        _sse(
            "content_block_start",
            {"index": 0, "content_block": {"type": "unmapped_future", "data": {}}},
        ),
        _sse(
            "content_block_delta",
            {"index": 0, "delta": {"type": "text_delta", "text": "tool output"}},
        ),
        _sse("content_block_stop", {"index": 0}),
        _sse(
            "content_block_start",
            {"index": 1, "content_block": {"type": "text", "text": ""}},
        ),
        _sse(
            "content_block_delta",
            {"index": 1, "delta": {"type": "text_delta", "text": "real answer"}},
        ),
        _sse("content_block_stop", {"index": 1}),
        _sse(
            "message_delta",
            {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
        ),
        _sse("message_stop", {}),
    ]
    events = await _collect(lines)
    deltas = [e.delta for e in events if e.delta is not None]
    assert deltas == ["real answer"]  # "tool output" never leaks into the answer
    assert any(e.extra_block_delta == {"type": "text_delta", "text": "tool output"} for e in events)


def test_to_message_uses_verbatim_ordered_content() -> None:
    """Interleaved text/server-tool order survives to_message verbatim."""
    from rekai.schemas import ChatResponse

    ordered = [
        {"type": "text", "text": "Searching now"},
        _SERVER_USE,
        _SEARCH_RESULT,
        {"type": "text", "text": "Found it"},
    ]
    resp = ChatResponse(
        id="x",
        provider="anthropic",
        model="claude-sonnet-4-6",
        content="Searching nowFound it",
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        created=0,
        content_blocks=ordered,
        extra_blocks=[_SERVER_USE, _SEARCH_RESULT],
    )
    msg = to_message(resp)
    assert msg["content"] == ordered


def test_history_echo_replays_verbatim_order() -> None:
    """An assistant turn echoes its exact upstream sequence, not the flattened order."""
    ordered = [
        {"type": "text", "text": "Searching now"},
        _SERVER_USE,
        _SEARCH_RESULT,
        {"type": "text", "text": "Found it"},
    ]
    req = AnthropicMessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=10,
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": ordered},
            {"role": "user", "content": "and?"},
        ],
    )
    chat = anthropic_compat.to_chat_request(req)
    assistant = chat.messages[1]
    assert assistant.content_blocks == ordered
    provider = AnthropicProvider()
    payload = provider._build_payload(chat, stream=False)
    assert payload["messages"][1]["content"] == ordered


async def test_redaction_scrubs_extra_block_strings() -> None:
    """Server-tool result text gets the same secret scrub as the answer."""
    secret_block = {
        "type": "web_search_tool_result",
        "tool_use_id": "srvtoolu_1",
        "content": [
            {"type": "web_search_result", "url": "https://x", "title": "key: ghp_" + "A" * 36}
        ],
    }

    class _SecretProvider(Provider):
        name = "svc-secret"

        async def chat(self, request, api_key):
            return ProviderResult(
                content="ok",
                model="x",
                usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                extra_blocks=[secret_block],
                content_blocks=[secret_block, {"type": "text", "text": "ok"}],
            )

        async def stream_events(self, request, api_key):
            yield StreamEvent(extra_block=secret_block)
            yield StreamEvent(delta="ok")
            yield StreamEvent(finish_reason="stop")

        async def embed(self, texts, model, api_key):
            raise ProviderError("unused")

    provider = _SecretProvider()
    register_provider(provider)
    settings = Settings(environment="test", default_provider="echo", output_redaction_enabled=True)
    result = await handle_chat(
        _req().model_copy(update={"model": "x", "provider": "svc-secret"}),
        None,
        settings,
        NullCache(),
    )
    title = result.extra_blocks[0]["content"][0]["title"]
    assert "ghp_" + "A" * 36 not in title
    assert "ghp_" + "A" * 36 not in result.content_blocks[0]["content"][0]["title"]


async def test_semantic_cache_skips_extra_blocks_history() -> None:
    """Histories carrying server-tool results can't collide on plain-text embeddings."""
    from rekai.semantic_cache import semantic_cache

    settings = Settings(
        environment="test",
        default_provider="echo",
        cache_enabled=False,
        semantic_cache_enabled=True,
        semantic_cache_model="echo",
    )
    semantic_cache.clear()
    request = ChatRequest(
        model="echo",
        messages=[
            ChatMessage(
                role="assistant",
                content="Done",
                extra_blocks=[_SERVER_USE],
            ),
            ChatMessage(role="user", content="Continue"),
        ],
    )
    first = await handle_chat(request, None, settings, NullCache())
    second = await handle_chat(request, None, settings, NullCache())
    assert first.cached is False
    assert second.cached is False
    semantic_cache.clear()


async def test_stream_extra_block_delta_merges_into_completed_block() -> None:
    """Deltas inside an extra block reach the completed extra_block, not just frames."""
    lines = [
        _sse(
            "content_block_start",
            {"index": 0, "content_block": {"type": "unmapped_future", "data": {}}},
        ),
        _sse(
            "content_block_delta",
            {"index": 0, "delta": {"type": "text_delta", "text": "tool "}},
        ),
        _sse(
            "content_block_delta",
            {"index": 0, "delta": {"type": "text_delta", "text": "output"}},
        ),
        _sse("content_block_stop", {"index": 0}),
        _sse("message_delta", {"delta": {"stop_reason": "end_turn"}}),
    ]
    events = await _collect(lines)
    done = [e.extra_block for e in events if e.extra_block]
    assert done[0]["text"] == "tool output"


def test_history_echo_content_blocks_takes_cache_control_on_a_copy() -> None:
    """Per-message and top-level cache_control mark the verbatim echo's last
    block — without mutating the caller's ordered array."""
    ordered = [{"type": "text", "text": "hi"}, dict(_SERVER_USE)]
    msg = ChatMessage(
        role="assistant",
        content="hi",
        content_blocks=ordered,
        cache_control={"type": "ephemeral"},
    )
    provider = AnthropicProvider()
    req = ChatRequest(model="claude-sonnet-4-6", messages=[msg])
    payload = provider._build_payload(req, stream=False)
    assert payload["messages"][0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in ordered[-1]

    req2 = ChatRequest(
        model="claude-sonnet-4-6",
        messages=[msg],
        cache_control={"type": "ephemeral", "ttl": "1h"},
    )
    payload2 = provider._build_payload(req2, stream=False)
    assert payload2["messages"][-1]["content"][-1]["cache_control"] == {
        "type": "ephemeral",
        "ttl": "1h",
    }
    assert "cache_control" not in ordered[-1]


def test_json_mode_strips_synthetic_tool_from_content_blocks(monkeypatch) -> None:
    """The forced json_response tool_use isn't part of the verbatim answer."""
    blocks = [
        {
            "type": "tool_use",
            "id": "tu_1",
            "name": "json_response",
            "input": {"answer": 42},
        },
        {"type": "text", "text": ""},
    ]
    _FakeClient.data = _resp(blocks)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    req = ChatRequest(
        model="claude-sonnet-4-6",
        messages=[ChatMessage(role="user", content="hi")],
        response_format={"type": "json_object"},
    )
    result = asyncio.run(AnthropicProvider().chat(req, "k"))
    assert result.content == '{"answer": 42}'
    assert result.content_blocks == [{"type": "text", "text": ""}]


def test_json_mode_keeps_other_blocks_in_content_blocks(monkeypatch) -> None:
    """Blocks unrelated to the synthetic tool still pass through in JSON mode."""
    blocks = [
        {"type": "tool_use", "id": "tu_1", "name": "json_response", "input": {"a": 1}},
        _SERVER_USE,
    ]
    _FakeClient.data = _resp(blocks)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    req = ChatRequest(
        model="claude-sonnet-4-6",
        messages=[ChatMessage(role="user", content="hi")],
        response_format={"type": "json_object"},
    )
    result = asyncio.run(AnthropicProvider().chat(req, "k"))
    assert result.content_blocks == [_SERVER_USE]


async def test_stream_split_secret_in_extra_delta_is_scrubbed() -> None:
    """A secret straddling two extra-block deltas can't leak in either frame."""
    secret = "ghp_" + "A" * 36
    frag1 = '{"key": "ghp_'
    frag2 = "A" * 36 + '"}'

    class _SplitSecretProvider(Provider):
        name = "svc-split"

        async def chat(self, request, api_key):
            raise ProviderError("unused")

        async def stream_events(self, request, api_key):
            yield StreamEvent(extra_block_start={"type": "server_tool_use", "input": {}})
            yield StreamEvent(extra_block_delta={"type": "input_json_delta", "partial_json": frag1})
            yield StreamEvent(extra_block_delta={"type": "input_json_delta", "partial_json": frag2})
            yield StreamEvent(extra_block={"type": "server_tool_use", "input": {"key": secret}})
            yield StreamEvent(delta="ok")
            yield StreamEvent(finish_reason="stop")

        async def embed(self, texts, model, api_key):
            raise ProviderError("unused")

    provider = _SplitSecretProvider()
    register_provider(provider)
    settings = Settings(environment="test", default_provider="echo", output_redaction_enabled=True)
    events = [
        e
        async for e in handle_chat_stream(
            ChatRequest(model="x", messages=[ChatMessage(role="user", content="hi")]),
            None,
            settings,
            NullCache(),
            "svc-split",
            provider,
            "anon",
        )
    ]
    frames = [e.extra_block_delta for e in events if e.extra_block_delta is not None]
    emitted = "".join(str(f.get("partial_json", "")) for f in frames)
    assert secret not in emitted
    block = [e.extra_block for e in events if e.extra_block is not None][0]
    assert secret not in json.dumps(block)
    summary = [e.summary for e in events if e.summary is not None][0]
    assert summary.redacted
