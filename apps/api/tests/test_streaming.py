"""Tests for streaming chat (SSE) and provider stream parsers."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from rekai.providers.anthropic import _parse_anthropic_sse_line
from rekai.providers.ollama import _parse_ollama_ndjson_line
from rekai.providers.openai import OpenAIProvider, _parse_openai_sse_line
from rekai.schemas import ChatMessage, ChatRequest


def _parse_sse(text: str) -> list[str]:
    """Return the list of `data:` payloads from an SSE response body."""
    out = []
    for line in text.splitlines():
        if line.startswith("data:"):
            out.append(line[len("data:") :].strip())
    return out


def _deltas(payloads: list[str]) -> str:
    chunks = []
    for p in payloads:
        if p == "[DONE]":
            continue
        chunks.append(json.loads(p).get("delta", ""))
    return "".join(chunks)


# --- endpoint (echo, native streaming) --------------------------------------


def test_stream_endpoint_echo(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat/stream",
        json={"model": "echo", "messages": [{"role": "user", "content": "hello world"}]},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-rekai-provider"] == "echo"

    payloads = _parse_sse(resp.text)
    assert payloads[-1] == "[DONE]"
    assert _deltas(payloads) == "Echo: hello world"
    # Native echo streaming emits more than one chunk.
    assert len([p for p in payloads if p != "[DONE]"]) > 1


def test_stream_endpoint_emits_usage_summary(client: TestClient) -> None:
    before = client.get("/v1/usage").json()["tokens_total"]
    resp = client.post(
        "/v1/chat/stream",
        json={"model": "echo", "messages": [{"role": "user", "content": "alpha beta"}]},
    )
    payloads = _parse_sse(resp.text)
    # The penultimate event (before [DONE]) is the usage summary.
    summary = json.loads(payloads[-2])
    assert summary["provider"] == "echo"
    # echo reports exact usage via stream_events -> not estimated.
    assert summary["estimated"] is False
    assert summary["usage"]["total_tokens"] > 0
    assert summary["cost_usd"] == 0.0  # echo is free
    # Streamed usage is now recorded in /v1/usage.
    after = client.get("/v1/usage").json()["tokens_total"]
    assert after > before


def test_stream_estimation_handles_none_content(client: TestClient) -> None:
    # Regression: a tools conversation carries messages with content=None. When
    # the provider doesn't report usage, the streaming path estimates tokens over
    # the messages — which must not crash on a None content.
    from rekai.providers import register_provider
    from rekai.providers.base import Provider, ProviderResult
    from rekai.schemas import Usage

    class NoUsageStream(Provider):
        name = "nousage-stream"
        requires_key = False

        async def chat(self, request, api_key) -> ProviderResult:
            # Base stream_events() wraps this and reports no usage.
            return ProviderResult(content="streamed reply", model=request.model, usage=Usage())

    register_provider(NoUsageStream())
    resp = client.post(
        "/v1/chat/stream",
        json={
            "provider": "nousage-stream",
            "model": "x",
            "messages": [
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "f", "arguments": "{}"},
                        }
                    ],
                },
            ],
        },
    )
    assert resp.status_code == 200
    payloads = _parse_sse(resp.text)
    assert payloads[-1] == "[DONE]"  # stream completed (no crash)
    summary = json.loads(payloads[-2])
    assert summary["estimated"] is True  # no provider usage -> estimated
    assert summary["usage"]["total_tokens"] > 0


def test_stream_429_marks_cooldown(client: TestClient) -> None:
    # A 429 seen on the streaming path must park the provider too (consistent
    # with non-streaming), so later requests route around it.
    from rekai.cooldown import cooldowns
    from rekai.providers import register_provider
    from rekai.providers.base import Provider, ProviderError, ProviderResult

    class RateLimited429(Provider):
        name = "rl429-stream"
        requires_key = False

        async def chat(self, request, api_key) -> ProviderResult:
            raise ProviderError("upstream rate limit", status_code=429, retry_after=30)

    register_provider(RateLimited429())
    cooldowns.clear()
    resp = client.post(
        "/v1/chat/stream",
        json={
            "provider": "rl429-stream",
            "model": "x",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    payloads = _parse_sse(resp.text)
    assert any("provider_error" in p for p in payloads)
    assert payloads[-1] == "[DONE]"
    assert cooldowns.active("rl429-stream") is True  # parked from the stream
    cooldowns.clear()


def test_stream_error_has_no_usage_summary(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat/stream",
        json={
            "provider": "openai",
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    payloads = _parse_sse(resp.text)
    assert not any('"usage"' in p for p in payloads)


def test_stream_endpoint_error_is_sent_as_event(client: TestClient) -> None:
    # openai with no key -> ProviderError surfaced inside the stream.
    resp = client.post(
        "/v1/chat/stream",
        json={
            "provider": "openai",
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200
    payloads = _parse_sse(resp.text)
    assert any("provider_error" in p for p in payloads)
    assert payloads[-1] == "[DONE]"


# --- base fallback (provider without native streaming) ----------------------


async def test_base_stream_fallback_single_chunk() -> None:
    from rekai.providers.base import Provider, ProviderResult
    from rekai.schemas import Usage

    class OneShot(Provider):
        name = "oneshot-test"
        requires_key = False

        async def chat(self, request, api_key) -> ProviderResult:
            return ProviderResult(content="all at once", model=request.model, usage=Usage())

    chunks = [
        c
        async for c in OneShot().stream(
            ChatRequest(model="x", messages=[ChatMessage(role="user", content="hi")]), None
        )
    ]
    assert chunks == ["all at once"]


async def test_base_stream_events_reports_no_usage() -> None:
    """The default stream_events wraps stream() and yields no usage event."""
    from rekai.providers.base import Provider, ProviderResult
    from rekai.schemas import Usage

    class OneShot(Provider):
        name = "oneshot-events-test"
        requires_key = False

        async def chat(self, request, api_key) -> ProviderResult:
            return ProviderResult(content="hi there", model=request.model, usage=Usage())

    events = [
        e
        async for e in OneShot().stream_events(
            ChatRequest(model="x", messages=[ChatMessage(role="user", content="hi")]), None
        )
    ]
    assert all(e.usage is None for e in events)
    assert "".join(e.delta or "" for e in events) == "hi there"


async def test_echo_stream_events_reports_exact_usage() -> None:
    from rekai.providers.echo import EchoProvider

    events = [
        e
        async for e in EchoProvider().stream_events(
            ChatRequest(model="echo", messages=[ChatMessage(role="user", content="a b c")]),
            None,
        )
    ]
    usage_events = [e for e in events if e.usage is not None]
    assert len(usage_events) == 1
    assert usage_events[0].usage.total_tokens > 0


# --- provider SSE/NDJSON line parsers ---------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        ('data: {"choices":[{"delta":{"content":"Hi"}}]}', "Hi"),
        ("data: [DONE]", None),
        ("", None),
        (": comment", None),
        ('data: {"choices":[]}', None),
    ],
)
def test_openai_sse_parser(line, expected) -> None:
    assert _parse_openai_sse_line(line) == expected


@pytest.mark.parametrize(
    "line,expected",
    [
        ('data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}', "Hi"),
        ('data: {"type":"message_start"}', None),
        ("event: ping", None),
        ("", None),
    ],
)
def test_anthropic_sse_parser(line, expected) -> None:
    assert _parse_anthropic_sse_line(line) == expected


@pytest.mark.parametrize(
    "line,expected",
    [
        ('{"message":{"content":"Hi"},"done":false}', "Hi"),
        ('{"message":{"content":""},"done":true}', None),
        ("", None),
        ("not json", None),
    ],
)
def test_ollama_ndjson_parser(line, expected) -> None:
    assert _parse_ollama_ndjson_line(line) == expected


def test_openai_sse_event_parses_usage() -> None:
    from rekai.providers.openai import _parse_openai_sse_event

    line = (
        'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":7,"total_tokens":12}}'
    )
    event = _parse_openai_sse_event(line)
    assert event is not None and event.usage is not None
    assert event.usage.total_tokens == 12
    # A delta line yields a delta event, not usage.
    delta_event = _parse_openai_sse_event('data: {"choices":[{"delta":{"content":"Hi"}}]}')
    assert delta_event is not None and delta_event.delta == "Hi" and delta_event.usage is None


def test_ollama_ndjson_event_parses_usage() -> None:
    from rekai.providers.ollama import _parse_ollama_ndjson_event

    line = '{"message":{"content":""},"done":true,"prompt_eval_count":4,"eval_count":6}'
    event = _parse_ollama_ndjson_event(line)
    assert event is not None and event.usage is not None
    assert event.usage.total_tokens == 10


# --- OpenAI native streaming with mocked HTTP -------------------------------


class _FakeStream:
    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self.status_code = status_code
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b"error body"


class _FakeClient:
    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self._lines = lines
        self._status = status_code

    def __init_subclass__(cls):  # pragma: no cover
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, **kwargs):
        return _FakeStream(self._lines, self._status)


async def test_openai_native_stream(monkeypatch) -> None:
    lines = [
        'data: {"choices":[{"delta":{"content":"Hello"}}]}',
        'data: {"choices":[{"delta":{"content":" world"}}]}',
        "data: [DONE]",
    ]
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(lines))

    req = ChatRequest(model="gpt-4o-mini", messages=[ChatMessage(role="user", content="hi")])
    chunks = [c async for c in OpenAIProvider().stream(req, api_key="sk-test")]
    assert "".join(chunks) == "Hello world"


async def test_openai_stream_events_surfaces_usage(monkeypatch) -> None:
    lines = [
        'data: {"choices":[{"delta":{"content":"Hello"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}',
        "data: [DONE]",
    ]
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(lines))

    req = ChatRequest(model="gpt-4o-mini", messages=[ChatMessage(role="user", content="hi")])
    events = [e async for e in OpenAIProvider().stream_events(req, api_key="sk-test")]
    deltas = "".join(e.delta or "" for e in events)
    usage = next((e.usage for e in events if e.usage is not None), None)
    assert deltas == "Hello"
    assert usage is not None and usage.total_tokens == 3


def test_openai_tool_call_accumulator() -> None:
    from rekai.providers.openai import _accumulate_tool_call_deltas

    acc: dict = {}
    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
        '"type":"function","function":{"name":"get_weather","arguments":""}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"{\\"ci"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"ty\\":\\"Tokyo\\"}"}}]}}]}',
    ]
    for ln in lines:
        _accumulate_tool_call_deltas(ln, acc)
    assert list(acc) == [0]
    assert acc[0]["id"] == "call_1"
    assert acc[0]["function"]["name"] == "get_weather"
    assert acc[0]["function"]["arguments"] == '{"city":"Tokyo"}'


async def test_openai_stream_events_assembles_tool_calls(monkeypatch) -> None:
    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
        '"type":"function","function":{"name":"get_weather","arguments":""}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"{\\"city\\":\\"Tokyo\\"}"}}]}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}',
        "data: [DONE]",
    ]
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(lines))

    req = ChatRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content="weather?")],
        tools=[{"type": "function", "function": {"name": "get_weather"}}],
    )
    events = [e async for e in OpenAIProvider().stream_events(req, api_key="sk-test")]
    tool_calls = next((e.tool_calls for e in events if e.tool_calls is not None), None)
    usage = next((e.usage for e in events if e.usage is not None), None)
    assert tool_calls is not None and len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "get_weather"
    assert tool_calls[0]["function"]["arguments"] == '{"city":"Tokyo"}'
    assert usage is not None and usage.total_tokens == 8


async def test_anthropic_stream_events_surfaces_usage(monkeypatch) -> None:
    from rekai.providers.anthropic import AnthropicProvider

    lines = [
        'data: {"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":0}}}',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}',
        'data: {"type":"message_delta","delta":{},"usage":{"output_tokens":4}}',
        'data: {"type":"message_stop"}',
    ]
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(lines))

    req = ChatRequest(model="claude-sonnet-4-6", messages=[ChatMessage(role="user", content="hi")])
    events = [e async for e in AnthropicProvider().stream_events(req, api_key="sk-ant")]
    assert "".join(e.delta or "" for e in events) == "Hi"
    usage = next((e.usage for e in events if e.usage is not None), None)
    assert usage is not None
    assert usage.prompt_tokens == 10 and usage.completion_tokens == 4 and usage.total_tokens == 14


async def test_gemini_stream_events_surfaces_usage(monkeypatch) -> None:
    from rekai.providers.gemini import GeminiProvider

    lines = [
        'data: {"candidates":[{"content":{"parts":[{"text":"Hi"}]}}]}',
        'data: {"candidates":[{"content":{"parts":[{"text":"!"}]}}],'
        '"usageMetadata":{"promptTokenCount":3,"candidatesTokenCount":2,"totalTokenCount":5}}',
    ]
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(lines))

    req = ChatRequest(model="gemini-1.5-flash", messages=[ChatMessage(role="user", content="hi")])
    events = [e async for e in GeminiProvider().stream_events(req, api_key="g-key")]
    assert "".join(e.delta or "" for e in events) == "Hi!"
    usage = next((e.usage for e in events if e.usage is not None), None)
    assert usage is not None and usage.total_tokens == 5
