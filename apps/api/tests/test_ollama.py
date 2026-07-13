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


async def test_tools_and_response_format_logged_not_forwarded(monkeypatch, caplog) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    req = ChatRequest(
        model="llama3",
        messages=[ChatMessage(role="user", content="weather?")],
        tools=[WEATHER_TOOL],
        response_format={"type": "json_object"},
    )
    with caplog.at_level(logging.DEBUG, logger="rekai.providers.ollama"):
        await OllamaProvider().chat(req, api_key=None)
    # Neither field is sent upstream — Ollama's /api/chat payload has no slot
    # for either, so this asserts they're silently dropped, not mistranslated.
    assert "tools" not in FakeClient.captured["json"]
    assert "response_format" not in FakeClient.captured["json"]
    messages = [r.message for r in caplog.records]
    assert any("ignoring tools" in m for m in messages)
    assert any("ignoring response_format" in m for m in messages)


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
