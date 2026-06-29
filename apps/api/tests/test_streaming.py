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
    assert summary["estimated"] is True
    assert summary["usage"]["total_tokens"] > 0
    assert summary["cost_usd"] == 0.0  # echo is free
    # Streamed usage is now recorded in /v1/usage.
    after = client.get("/v1/usage").json()["tokens_total"]
    assert after > before


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
