"""`finish_reason` propagation from every provider.

Every backend reports why generation stopped — OpenAI `finish_reason`,
Anthropic `stop_reason`, Gemini `finishReason`, Ollama `done_reason` — and RekAI
used to discard all four, synthesising `"stop"` at the edge
(`openai_compat.to_chat_completion`). The consequence was not cosmetic: an
answer **cut off by max_tokens** was reported as an ordinary completion, so the
standard "retry with a larger budget when finish_reason == 'length'" pattern
could never fire, and the truncated answer was cached and replayed as if whole.

These tests pin the normalization table and the two paths (non-streaming and
streaming) for each provider.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from rekai.config import Settings
from rekai.main import create_app
from rekai.providers import register_provider
from rekai.providers.anthropic import AnthropicProvider
from rekai.providers.base import Provider, ProviderResult
from rekai.providers.base import StreamEvent as ProviderStreamEvent
from rekai.providers.gemini import GeminiProvider
from rekai.providers.ollama import OllamaProvider
from rekai.providers.openai import OpenAIProvider, _parse_openai_sse_event
from rekai.schemas import ChatMessage, ChatRequest, Usage


def _req(model: str = "m", **kw) -> ChatRequest:
    kw.setdefault("messages", [ChatMessage(role="user", content="hi")])
    return ChatRequest(model=model, **kw)


def _fake_post(monkeypatch, payload: dict) -> None:
    class FakeResponse:
        status_code = 200
        headers: dict = {}

        def json(self) -> dict:
            return payload

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:  # pragma: no cover - error path only
        return b""


def _fake_stream(monkeypatch, lines: list[str]) -> None:
    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, **kwargs):
            return _FakeStream(lines)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)


async def _stream_finish(provider, request) -> str | None:
    reason = None
    async for ev in provider.stream_events(request, api_key="k"):
        if ev.finish_reason is not None:
            reason = ev.finish_reason
    return reason


# --- OpenAI ------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("stop", "stop"),
        ("length", "length"),
        ("tool_calls", "tool_calls"),
        ("content_filter", "content_filter"),
        ("function_call", None),  # deprecated API value: not guessed at
        ("something_new", None),
        (None, None),
    ],
)
async def test_openai_maps_finish_reason(monkeypatch, raw, expected) -> None:
    _fake_post(
        monkeypatch,
        {
            "model": "gpt-4o",
            "choices": [{"message": {"content": "hi"}, "finish_reason": raw}],
            "usage": {},
        },
    )
    result = await OpenAIProvider().chat(_req("gpt-4o"), api_key="k")
    assert result.finish_reason == expected


def test_openai_streaming_terminal_chunk_carries_the_reason() -> None:
    # The terminal chunk has an empty delta and only the finish_reason; before
    # this it produced no event at all and the reason was lost.
    event = _parse_openai_sse_event('data: {"choices":[{"delta":{},"finish_reason":"length"}]}')
    assert event is not None
    assert event.finish_reason == "length"
    assert event.delta is None


# --- Anthropic ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("tool_use", "tool_calls"),
        ("refusal", "content_filter"),
        ("something_new", None),
        (None, None),
    ],
)
async def test_anthropic_maps_stop_reason(monkeypatch, raw, expected) -> None:
    _fake_post(
        monkeypatch,
        {
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": raw,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    result = await AnthropicProvider().chat(_req("claude-sonnet-4-6"), api_key="k")
    assert result.finish_reason == expected


async def test_anthropic_json_emulation_reports_stop_not_tool_calls(monkeypatch) -> None:
    # The forced `json_response` tool is RekAI's own device for structured
    # output. Anthropic says stop_reason=tool_use, but the caller asked for JSON
    # content and never sees a tool call — reporting "tool_calls" would describe
    # an implementation detail.
    _fake_post(
        monkeypatch,
        {
            "model": "claude-sonnet-4-6",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "json_response", "input": {"a": 1}}
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )
    result = await AnthropicProvider().chat(
        _req("claude-sonnet-4-6", response_format={"type": "json_object"}), api_key="k"
    )
    assert result.finish_reason == "stop"
    assert result.tool_calls is None


async def test_anthropic_streaming_reads_stop_reason_from_message_delta(monkeypatch) -> None:
    _fake_stream(
        monkeypatch,
        [
            'data: {"type":"message_start","message":{"usage":{"input_tokens":5}}}',
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"hi"}}',
            'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},'
            '"usage":{"output_tokens":9}}',
        ],
    )
    assert await _stream_finish(AnthropicProvider(), _req("claude-sonnet-4-6")) == "length"


# --- Gemini ------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("STOP", "stop"),
        ("MAX_TOKENS", "length"),
        ("SAFETY", "content_filter"),
        ("RECITATION", "content_filter"),
        ("PROHIBITED_CONTENT", "content_filter"),
        ("SOMETHING_NEW", None),
        (None, None),
    ],
)
async def test_gemini_maps_finish_reason(monkeypatch, raw, expected) -> None:
    candidate: dict = {"content": {"parts": [{"text": "hi"}]}}
    if raw is not None:
        candidate["finishReason"] = raw
    _fake_post(monkeypatch, {"candidates": [candidate], "usageMetadata": {}})
    result = await GeminiProvider().chat(_req("gemini-1.5-pro"), api_key="k")
    assert result.finish_reason == expected


async def test_gemini_tool_call_reports_tool_calls_despite_saying_stop(monkeypatch) -> None:
    # Gemini says STOP even when it stopped to emit a functionCall, so the
    # reason alone would tell an OpenAI client nothing had to be run.
    _fake_post(
        monkeypatch,
        {
            "candidates": [
                {
                    "content": {"parts": [{"functionCall": {"name": "f", "args": {}}}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {},
        },
    )
    result = await GeminiProvider().chat(_req("gemini-1.5-pro"), api_key="k")
    assert result.finish_reason == "tool_calls"


# --- Ollama ------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected", [("stop", "stop"), ("length", "length"), ("unload", None), (None, None)]
)
async def test_ollama_maps_done_reason(monkeypatch, raw, expected) -> None:
    payload: dict = {"model": "llama3", "message": {"content": "hi"}}
    if raw is not None:
        payload["done_reason"] = raw
    _fake_post(monkeypatch, payload)
    result = await OllamaProvider().chat(_req("llama3"), api_key=None)
    assert result.finish_reason == expected


async def test_ollama_streaming_reads_done_reason(monkeypatch) -> None:
    _fake_stream(
        monkeypatch,
        [
            '{"message":{"content":"hi"},"done":false}',
            '{"done":true,"done_reason":"length","prompt_eval_count":3,"eval_count":4}',
        ],
    )
    assert await _stream_finish(OllamaProvider(), _req("llama3")) == "length"


# --- end to end --------------------------------------------------------------
# The value has to survive every layer between the provider and the caller:
# service -> ChatResponse -> cache/idempotency storage -> OpenAI translation.


class TruncatingProvider(Provider):
    """A provider whose answer is always cut off by the token budget."""

    name = "truncating"
    requires_key = False

    async def chat(self, request, api_key) -> ProviderResult:
        return ProviderResult(
            content="a partial ans", model=request.model, usage=Usage(), finish_reason="length"
        )

    async def stream_events(self, request, api_key):
        yield ProviderStreamEvent(delta="a partial ans")
        yield ProviderStreamEvent(usage=Usage(), finish_reason="length")

    async def embed(self, inputs, model, api_key):  # pragma: no cover - unused
        raise NotImplementedError

    async def list_models(self, api_key):
        return ["truncating"]

    async def list_embedding_models(self, api_key):
        return []


def _truncating_client(**kw) -> TestClient:
    register_provider(TruncatingProvider())
    return TestClient(
        create_app(
            Settings(
                environment="test",
                default_provider="truncating",
                rate_limit_enabled=False,
                **kw,
            )
        )
    )


def test_native_endpoint_reports_truncation() -> None:
    client = _truncating_client()
    body = {"model": "truncating", "messages": [{"role": "user", "content": "hi"}]}
    assert client.post("/v1/chat", json=body).json()["finish_reason"] == "length"


def test_truncation_survives_a_cache_hit() -> None:
    # A truncated answer that is cached must keep saying it was truncated —
    # otherwise the first caller learns the truth and everyone after doesn't.
    client = _truncating_client()
    body = {"model": "truncating", "messages": [{"role": "user", "content": "cache me"}]}
    first = client.post("/v1/chat", json=body).json()
    second = client.post("/v1/chat", json=body).json()
    assert second["cached"] is True
    assert first["finish_reason"] == second["finish_reason"] == "length"


def test_truncation_survives_an_idempotent_replay() -> None:
    client = _truncating_client()
    body = {"model": "truncating", "messages": [{"role": "user", "content": "idem"}]}
    headers = {"Idempotency-Key": "finish-reason-1"}
    client.post("/v1/chat", json=body, headers=headers)
    replay = client.post("/v1/chat", json=body, headers=headers)
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json()["finish_reason"] == "length"


def test_openai_compatible_endpoint_reports_length() -> None:
    # The case the whole change exists for: an OpenAI SDK checking
    # choices[0].finish_reason == "length" to retry with a bigger budget.
    client = _truncating_client()
    body = {"model": "truncating", "messages": [{"role": "user", "content": "hi"}]}
    resp = client.post("/v1/chat/completions", json=body).json()
    assert resp["choices"][0]["finish_reason"] == "length"


def test_openai_compatible_stream_finish_chunk_reports_length() -> None:
    client = _truncating_client()
    body = {"model": "truncating", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    with client.stream("POST", "/v1/chat/completions", json=body) as resp:
        raw = "".join(resp.iter_text())
    chunks = [
        json.loads(line[len("data: ") :])
        for line in raw.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    reasons = [c["choices"][0]["finish_reason"] for c in chunks if c.get("choices")]
    assert "length" in reasons


def test_native_stream_summary_reports_length() -> None:
    client = _truncating_client()
    body = {"model": "truncating", "messages": [{"role": "user", "content": "hi"}]}
    with client.stream("POST", "/v1/chat/stream", json=body) as resp:
        raw = "".join(resp.iter_text())
    events = [
        json.loads(line[len("data: ") :])
        for line in raw.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert any(e.get("finish_reason") == "length" for e in events)
