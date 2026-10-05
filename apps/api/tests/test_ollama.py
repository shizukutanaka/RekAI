"""Tests for the Ollama provider (chat, streaming, and unsupported-field logging).

Ollama previously had only incidental coverage (via test_embeddings.py's fake
embed test, plus generic router/streaming-endpoint tests using the echo
provider); this exercises OllamaProvider directly."""

from __future__ import annotations

import logging

import httpx
import pytest

from rekai.providers.base import ProviderError
from rekai.providers.ollama import OllamaProvider, _parse_ollama_ndjson_event
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


# --- error paths: embeddings, stream edges, thin stream() wrapper --------------
#
# The embed and stream error paths above are the branches a client actually
# sees when the local daemon is down or the model name is wrong — a raw httpx
# error or a collapsed status here surfaces as an opaque 500 at the edge.


class _ErrorResponse:
    """The fields provider_http_error reads: status_code, text, headers."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = "upstream said no"
        self.headers: dict = {}


class EmbedErrorClient:
    """POST answers with an upstream HTTP error status."""

    def __init__(self, *a, **k) -> None:
        pass

    async def post(self, url, json, headers):
        return _ErrorResponse(500)


class EmbedUnreachableClient:
    """POST fails at the transport layer (daemon down / DNS)."""

    def __init__(self, *a, **k) -> None:
        pass

    async def post(self, url, json, headers):
        raise httpx.ConnectError("connection refused")


async def test_embed_upstream_http_error_is_mapped(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", EmbedErrorClient)
    with pytest.raises(ProviderError) as err:
        await OllamaProvider().embed(["hi"], model="nomic-embed-text", api_key=None)
    assert err.value.status_code == 502  # upstream 5xx normalised to bad gateway


async def test_embed_transport_failure_is_wrapped(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", EmbedUnreachableClient)
    with pytest.raises(ProviderError):
        await OllamaProvider().embed(["hi"], model="nomic-embed-text", api_key=None)


class StreamingErrorClient:
    """The upstream stream opens with an HTTP error status."""

    def __init__(self, *a, **k) -> None:
        pass

    def stream(self, method, url, json=None, headers=None):
        class _Resp:
            status_code = 404

            async def aread(self) -> bytes:
                return b"model not found"

        class _Ctx:
            async def __aenter__(self):
                return _Resp()

            async def __aexit__(self, *a):
                return False

        return _Ctx()


class StreamingUnreachableClient:
    """The streaming request fails at the transport layer."""

    def __init__(self, *a, **k) -> None:
        pass

    def stream(self, method, url, json=None, headers=None):
        raise httpx.ConnectError("connection refused")


async def test_stream_upstream_error_status_propagates(monkeypatch) -> None:
    """A 4xx is the caller's problem, not the gateway's — the status must
    survive the wrap instead of collapsing into 502."""
    monkeypatch.setattr(httpx, "AsyncClient", StreamingErrorClient)
    req = ChatRequest(model="llama3", messages=[ChatMessage(role="user", content="hi")])
    with pytest.raises(ProviderError) as err:
        async for _ in OllamaProvider().stream_events(req, api_key=None):
            pass
    assert err.value.status_code == 404
    assert "model not found" in str(err.value)


async def test_stream_transport_failure_is_wrapped(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", StreamingUnreachableClient)
    req = ChatRequest(model="llama3", messages=[ChatMessage(role="user", content="hi")])
    with pytest.raises(ProviderError):
        async for _ in OllamaProvider().stream_events(req, api_key=None):
            pass


async def test_stream_wrapper_yields_only_text_deltas(monkeypatch) -> None:
    """`stream()` is the text-only view of `stream_events` — the thin iterator
    most call sites use."""
    monkeypatch.setattr(httpx, "AsyncClient", StreamingFakeClient)
    req = ChatRequest(model="llama3", messages=[ChatMessage(role="user", content="hi")])
    assert [d async for d in OllamaProvider().stream(req, api_key=None)] == ["hi"]


async def test_streaming_sends_format_for_constrained_decoding(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", StreamingFakeClient)
    req = ChatRequest(
        model="llama3",
        messages=[ChatMessage(role="user", content="hi")],
        response_format={"type": "json_object"},
    )
    async for _ in OllamaProvider().stream_events(req, api_key=None):
        pass
    assert StreamingFakeClient.captured["json"]["format"] == "json"


def test_parse_ndjson_event_done_reason_without_counts() -> None:
    """Ollama can report done_reason with no eval counts — the finish reason
    must survive on its own rather than being dropped with the usage."""
    event = _parse_ollama_ndjson_event('{"done": true, "done_reason": "length"}')
    assert event is not None
    assert event.finish_reason == "length"
    assert event.usage is None


def test_parse_ndjson_event_skips_blank_lines() -> None:
    """Keep-alive whitespace in an NDJSON stream must not yield a phantom event."""
    assert _parse_ollama_ndjson_event("") is None
    assert _parse_ollama_ndjson_event("   ") is None


def test_parse_ndjson_event_skips_unparseable_lines() -> None:
    """A truncated/corrupt chunk mid-stream must not abort the whole stream."""
    assert _parse_ollama_ndjson_event('{"message": {"content": "cut o') is None


def test_parse_ndjson_event_skips_lines_without_delta_or_done() -> None:
    """Non-message metadata lines (e.g. load stats) carry no delta — skip."""
    assert _parse_ollama_ndjson_event('{"done": false, "model": "llama3"}') is None


class EmbedClient:
    """Captures the embed payload; replays a normal /api/embed response."""

    def __init__(self, *a, **k) -> None:
        pass

    async def post(self, url, json, headers):
        EmbedClient.captured = {"url": url, "json": json}
        return FakeResponse(
            {"model": "nomic-embed-text", "embeddings": [[0.1, 0.2]], "prompt_eval_count": 3}
        )


async def test_options_forward_stop_top_p_seed(monkeypatch) -> None:
    """stop/top_p/seed are all declared ChatRequest fields and Ollama supports
    each under its own name — they must not be dropped like tools were."""
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    req = ChatRequest(
        model="llama3",
        messages=[ChatMessage(role="user", content="hi")],
        stop=["END"],
        top_p=0.5,
        seed=7,
    )
    await OllamaProvider().chat(req, api_key=None)
    options = FakeClient.captured["json"]["options"]
    assert options["stop"] == ["END"]
    assert options["top_p"] == pytest.approx(0.5)
    assert options["seed"] == 7


async def test_embed_maps_prompt_eval_count_to_usage(monkeypatch) -> None:
    """Ollama reports prompt_eval_count; usage must carry it as prompt+total
    tokens or the request silently bills zero."""
    monkeypatch.setattr(httpx, "AsyncClient", EmbedClient)
    result = await OllamaProvider().embed(["hi"], model="nomic-embed-text", api_key=None)
    assert result.embeddings == [[0.1, 0.2]]
    assert result.usage is not None
    assert result.usage.prompt_tokens == 3
    assert result.usage.total_tokens == 3
    assert EmbedClient.captured["json"]["input"] == ["hi"]
