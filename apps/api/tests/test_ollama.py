"""Tests for the Ollama provider (chat, streaming, and unsupported-field logging).

Ollama previously had only incidental coverage (via test_embeddings.py's fake
embed test, plus generic router/streaming-endpoint tests using the echo
provider); this exercises OllamaProvider directly."""

from __future__ import annotations

import logging

import httpx
import pytest

from rekai.providers.base import ProviderError
from rekai.providers.ollama import (
    OllamaProvider,
    _parse_ollama_ndjson_event,
    _parse_ollama_ndjson_line,
)
from rekai.schemas import ChatMessage, ChatRequest

WEATHER_TOOL = {
    "type": "function",
    "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {}}},
}


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeClient:
    def __init__(self, *a, **k) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json, headers):
        FakeClient.captured = {"url": url, "json": json}
        return FakeResponse(
            {
                "model": "llama3",
                "message": {"content": "Hello from Ollama"},
                "prompt_eval_count": 4,
                "eval_count": 3,
            }
        )


async def test_chat_parses_response(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    req = ChatRequest(model="llama3", messages=[ChatMessage(role="user", content="hi")])
    result = await OllamaProvider().chat(req, api_key=None)
    assert result.content == "Hello from Ollama"
    assert result.usage.prompt_tokens == 4
    assert result.usage.completion_tokens == 3
    assert result.usage.total_tokens == 7
    assert FakeClient.captured["url"].endswith("/api/chat")
    assert FakeClient.captured["json"]["stream"] is False


async def test_chat_no_key_required(monkeypatch) -> None:
    # Ollama is keyless; a request with no api_key must still succeed.
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    assert OllamaProvider().requires_key is False
    req = ChatRequest(model="llama3", messages=[ChatMessage(role="user", content="hi")])
    result = await OllamaProvider().chat(req, api_key=None)
    assert result.content


async def test_chat_raises_on_http_error(monkeypatch) -> None:
    class ErrClient(FakeClient):
        async def post(self, url, json, headers):
            return type("R", (), {"status_code": 500, "text": "boom", "headers": {}})()

    monkeypatch.setattr(httpx, "AsyncClient", ErrClient)
    req = ChatRequest(model="llama3", messages=[ChatMessage(role="user", content="hi")])
    with pytest.raises(ProviderError):
        await OllamaProvider().chat(req, api_key=None)


async def test_chat_network_error_wrapped(monkeypatch) -> None:
    class BrokenClient(FakeClient):
        async def post(self, url, json, headers):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "AsyncClient", BrokenClient)
    req = ChatRequest(model="llama3", messages=[ChatMessage(role="user", content="hi")])
    with pytest.raises(ProviderError, match="is it running"):
        await OllamaProvider().chat(req, api_key=None)


async def test_tools_are_logged_not_forwarded(monkeypatch, caplog) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    req = ChatRequest(
        model="llama3",
        messages=[ChatMessage(role="user", content="weather?")],
        tools=[WEATHER_TOOL],
    )
    with caplog.at_level(logging.DEBUG, logger="rekai.providers.ollama"):
        await OllamaProvider().chat(req, api_key=None)
    # tools are still not wired up for Ollama; assert they're dropped with a
    # trace rather than mistranslated.
    assert "tools" not in FakeClient.captured["json"]
    assert any("ignoring tools" in r.message for r in caplog.records)


# --- structured output -------------------------------------------------------
# Ollama's /api/chat takes a top-level `format`: "json" for free-form JSON, or a
# JSON schema for constrained decoding. RekAI used to log "unsupported by the
# ollama provider" and drop it, which was not true — it was unimplemented.


async def test_json_object_maps_to_format_json(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    req = ChatRequest(
        model="llama3",
        messages=[ChatMessage(role="user", content="hi")],
        response_format={"type": "json_object"},
    )
    await OllamaProvider().chat(req, api_key=None)
    assert FakeClient.captured["json"]["format"] == "json"


async def test_json_schema_is_sent_for_constrained_decoding(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    schema = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    req = ChatRequest(
        model="llama3",
        messages=[ChatMessage(role="user", content="hi")],
        response_format={"type": "json_schema", "json_schema": {"name": "loc", "schema": schema}},
    )
    await OllamaProvider().chat(req, api_key=None)
    # The schema goes through verbatim: Ollama constrains decoding to it, so the
    # output conforms by construction rather than by instruction.
    assert FakeClient.captured["json"]["format"] == schema


async def test_json_schema_without_a_schema_still_asks_for_json(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    req = ChatRequest(
        model="llama3",
        messages=[ChatMessage(role="user", content="hi")],
        response_format={"type": "json_schema"},
    )
    await OllamaProvider().chat(req, api_key=None)
    assert FakeClient.captured["json"]["format"] == "json"


async def test_text_format_and_absent_format_send_nothing(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    for rf in (None, {"type": "text"}):
        req = ChatRequest(
            model="llama3",
            messages=[ChatMessage(role="user", content="hi")],
            response_format=rf,
        )
        await OllamaProvider().chat(req, api_key=None)
        assert "format" not in FakeClient.captured["json"]


async def test_no_unsupported_fields_no_log(monkeypatch, caplog) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    req = ChatRequest(model="llama3", messages=[ChatMessage(role="user", content="hi")])
    with caplog.at_level(logging.DEBUG, logger="rekai.providers.ollama"):
        await OllamaProvider().chat(req, api_key=None)
    assert caplog.records == []


def test_parse_ndjson_event_delta() -> None:
    line = '{"message": {"content": "hel"}, "done": false}'
    event = _parse_ollama_ndjson_event(line)
    assert event is not None
    assert event.delta == "hel"


def test_parse_ndjson_event_final_usage() -> None:
    line = '{"done": true, "prompt_eval_count": 5, "eval_count": 2}'
    event = _parse_ollama_ndjson_event(line)
    assert event is not None
    assert event.usage is not None
    assert event.usage.total_tokens == 7


def test_parse_ndjson_line_delegates_to_event() -> None:
    assert _parse_ollama_ndjson_line('{"message": {"content": "x"}}') == "x"
    assert _parse_ollama_ndjson_line("") is None
    assert _parse_ollama_ndjson_line("not json") is None


# --- max_tokens ---------------------------------------------------------------
#
# `max_tokens` is a declared field of RekAI's own ChatRequest, and OpenAI,
# Anthropic and Gemini all forwarded it. Ollama did not send it under any name,
# so the cap was accepted and silently discarded and the local model generated
# until it stopped on its own. It is not an unsupported field — Ollama spells it
# `options.num_predict` — so it did not even reach the `_warn_unsupported_fields`
# log that exists precisely to stop fields being "dropped without a trace".


class StreamingFakeClient:
    """Captures the streaming payload; replays one delta and a final usage line."""

    captured: dict = {}

    def __init__(self, *a, **k) -> None:
        pass

    def stream(self, method, url, json=None, headers=None):
        StreamingFakeClient.captured = {"url": url, "json": json}

        class _Resp:
            status_code = 200

            async def aiter_lines(self):
                yield '{"message": {"content": "hi"}, "done": false}'
                yield '{"done": true, "prompt_eval_count": 1, "eval_count": 1}'

            async def aread(self) -> bytes:
                return b""

        class _Ctx:
            async def __aenter__(self):
                return _Resp()

            async def __aexit__(self, *a):
                return False

        return _Ctx()


async def test_max_tokens_is_sent_as_num_predict(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    await OllamaProvider().chat(
        ChatRequest(
            model="llama3",
            messages=[ChatMessage(role="user", content="write an essay")],
            max_tokens=16,
        ),
        api_key=None,
    )
    assert FakeClient.captured["json"]["options"]["num_predict"] == 16


async def test_streaming_sends_num_predict_too(monkeypatch) -> None:
    """The two payloads are built by one helper, so they cannot drift apart —
    which is how they came to differ from the other providers in the first
    place."""
    monkeypatch.setattr(httpx, "AsyncClient", StreamingFakeClient)
    req = ChatRequest(
        model="llama3",
        messages=[ChatMessage(role="user", content="write an essay")],
        max_tokens=16,
    )
    deltas = [ev.delta async for ev in OllamaProvider().stream_events(req, api_key=None)]
    assert "hi" in deltas
    assert StreamingFakeClient.captured["json"]["options"]["num_predict"] == 16


async def test_no_max_tokens_leaves_ollamas_own_default_alone(monkeypatch) -> None:
    """Absent means absent: sending num_predict unconditionally would override
    the model's configured default with whatever RekAI happened to pick."""
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    await OllamaProvider().chat(
        ChatRequest(model="llama3", messages=[ChatMessage(role="user", content="hi")]),
        api_key=None,
    )
    options = FakeClient.captured["json"]["options"]
    assert "num_predict" not in options
    assert options["temperature"] == pytest.approx(0.7)
