"""Anthropic web-search citations end-to-end.

When the web_search server tool grounds an answer, Anthropic hangs a
``citations`` array on the text block — and streams each citation as a
``citations_delta`` inside it. RekAI used to parse only the text and drop the
sources entirely: a client that turned on web search got answers without
attribution. Citations now ride the response, the typed stream, and the
compat SSE — each verbatim, each self-locating via its ``cited_text``.
"""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from rekai.anthropic_compat import to_message
from rekai.cache import NullCache
from rekai.config import Settings
from rekai.providers import register_provider
from rekai.providers.anthropic import AnthropicProvider
from rekai.providers.base import Provider, ProviderResult, StreamEvent
from rekai.providers.echo import EchoProvider
from rekai.schemas import ChatMessage, ChatRequest, ChatResponse, Usage
from rekai.service import handle_chat, handle_chat_stream


def _req(**kw: object) -> ChatRequest:
    kw.setdefault("model", "claude-sonnet-4-6")
    kw.setdefault("messages", [ChatMessage(role="user", content="hi")])
    return ChatRequest(**kw)  # type: ignore[arg-type]


_CITATION = {
    "type": "web_search_result_location",
    "url": "https://example.com/article",
    "title": "Example Article",
    "cited_text": "the cited claim",
}


class _Resp:
    status_code = 200
    headers: dict = {}
    text = ""

    def json(self) -> dict:
        return {
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "answer", "citations": [_CITATION]}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }


class _Client:
    captured: dict = {}

    def __init__(self, *a: object, **k: object) -> None:
        pass

    async def aclose(self) -> None:
        return None

    async def post(self, url, json=None, headers=None, **kw):
        _Client.captured = json or {}
        return _Resp()


# --- provider parse ----------------------------------------------------------


async def test_citations_reach_provider_result(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    result = await AnthropicProvider().chat(_req(), api_key="sk-ant")
    assert result.citations == [_CITATION]


async def test_no_citations_means_none(monkeypatch) -> None:
    class _NoCite(_Resp):
        def json(self) -> dict:
            return {
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "answer"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }

    class _NoCiteClient(_Client):
        async def post(self, url, json=None, headers=None, **kw):
            return _NoCite()

    monkeypatch.setattr(httpx, "AsyncClient", _NoCiteClient)
    result = await AnthropicProvider().chat(_req(), api_key="sk-ant")
    assert result.citations is None


async def test_citations_delta_streams_as_citation_event(monkeypatch) -> None:
    from tests.test_streaming import _FakeClient

    lines = [
        'data: {"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":0}}}',
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"answer"}}',
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"citations_delta","citation":{"type":"web_search_result_location",'
        '"url":"https://example.com","title":"Ex","cited_text":"answer"}}}',
        'data: {"type":"message_delta","delta":{},"usage":{"output_tokens":4}}',
        'data: {"type":"message_stop"}',
    ]
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(lines))
    events = [e async for e in AnthropicProvider().stream_events(_req(), api_key="sk-ant")]
    citation = next((e.citation for e in events if e.citation is not None), None)
    assert citation is not None
    assert citation["url"] == "https://example.com"


# --- compat + service --------------------------------------------------------


def test_to_message_attaches_citations_to_the_text_block() -> None:
    resp = ChatResponse(
        id="r1",
        provider="anthropic",
        model="m",
        content="answer",
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        citations=[_CITATION],
        created=0,
    )
    msg = to_message(resp)
    text_block = next(b for b in msg["content"] if b["type"] == "text")
    assert text_block["citations"] == [_CITATION]


async def test_citation_cited_text_is_secret_scrubbed(monkeypatch) -> None:
    """The citation echoes model-generated text — a secret redacted from the
    answer must not re-leak through the citation's quoted span."""
    secret = "sk-" + "e" * 30
    cite = {**_CITATION, "cited_text": f"your key is {secret} now"}

    async def fake_chat(self, request, api_key):
        return ProviderResult(
            model="echo",
            content="safe answer",
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            citations=[cite],
        )

    monkeypatch.setattr(EchoProvider, "chat", fake_chat)
    result = await handle_chat(
        ChatRequest(model="echo", messages=[ChatMessage(role="user", content="hi")]),
        None,
        Settings(environment="test", default_provider="echo", output_redaction_enabled=True),
        NullCache(),
    )
    assert secret not in result.citations[0]["cited_text"]
    assert "openai_api_key" in result.redacted


class _CitingProvider(Provider):
    """Streams a citation whose cited_text carries a secret — the stream-time
    scrub must catch it the same way it catches one in the text deltas."""

    name = "svc-citing"
    requires_key = False

    async def chat(self, request, api_key):  # pragma: no cover - unused here
        raise NotImplementedError

    async def stream_events(self, request, api_key):
        yield StreamEvent(delta="answer")
        yield StreamEvent(citation={**_CITATION, "cited_text": "key " + "sk-" + "e" * 30})
        yield StreamEvent(
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            finish_reason="stop",
        )


async def test_stream_citation_cited_text_is_scrubbed() -> None:
    provider = _CitingProvider()
    register_provider(provider)
    events = [
        e
        async for e in handle_chat_stream(
            ChatRequest(
                model="x",
                provider="svc-citing",
                messages=[ChatMessage(role="user", content="hi")],
            ),
            None,
            Settings(environment="test", default_provider="echo", output_redaction_enabled=True),
            NullCache(),
            "svc-citing",
            provider,
            "client-a",
        )
    ]
    citation = next(e.citation for e in events if e.citation is not None)
    assert "sk-" not in citation["cited_text"]
    assert "[REDACTED:openai_api_key]" in citation["cited_text"]


# --- /v1/messages stream -----------------------------------------------------


def test_stream_citation_reaches_anthropic_sse(client: TestClient, monkeypatch) -> None:
    """A citation upstream becomes a citations_delta inside the text block."""

    async def fake_stream_events(self, request, api_key):
        yield StreamEvent(delta="answer")
        yield StreamEvent(citation=_CITATION)
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
    delta_types = [data["delta"]["type"] for name, data in events if name == "content_block_delta"]
    assert "citations_delta" in delta_types
    cite_event = next(
        data
        for name, data in events
        if name == "content_block_delta" and data["delta"]["type"] == "citations_delta"
    )
    assert cite_event["delta"]["citation"]["url"] == "https://example.com/article"


def test_stream_citation_reaches_native_sse(client: TestClient, monkeypatch) -> None:
    """The RekAI-native stream emits a citation frame."""

    async def fake_stream_events(self, request, api_key):
        yield StreamEvent(delta="answer")
        yield StreamEvent(citation=_CITATION)
        yield StreamEvent(finish_reason="stop")

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
    assert any("citation" in f for f in frames)
