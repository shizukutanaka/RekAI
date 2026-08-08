"""Structured output (`response_format`) across providers.

RekAI advertises `response_format` in its OpenAPI schema and passes it to
providers, but two of them used to accept it and drop it with only a debug log —
so a caller who asked for JSON got prose and no signal. That breaks the one
promise a gateway makes: a request written against the OpenAI API behaves the
same way whichever backend serves it. Neither provider actually lacked the
capability; RekAI just hadn't wired it up.

* **Ollama** takes a top-level `format`: `"json"`, or a JSON schema it uses for
  constrained decoding.
* **Anthropic** has no `response_format` at all, but its documented route to a
  schema-shaped answer is *forced tool use* — one tool whose `input_schema` is
  the desired shape, with `tool_choice` pinned to it. RekAI unwraps the
  resulting `tool_use` block back into JSON `content`, so the response looks
  like OpenAI's JSON mode rather than like a tool call the caller never asked for.
"""

from __future__ import annotations

import json

import httpx
import pytest

from rekai.providers.anthropic import _JSON_TOOL_NAME, AnthropicProvider
from rekai.schemas import ChatMessage, ChatRequest

SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "temp_c": {"type": "number"}},
    "required": ["city"],
}
JSON_SCHEMA_FORMAT = {"type": "json_schema", "json_schema": {"name": "weather", "schema": SCHEMA}}
WEATHER_TOOL = {
    "type": "function",
    "function": {"name": "get_weather", "parameters": {"type": "object"}},
}


def _fake_post(monkeypatch, captured: dict, payload: dict) -> None:
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
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)


def _reply(blocks: list[dict]) -> dict:
    return {
        "model": "claude-sonnet-4-6",
        "content": blocks,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _req(**kw) -> ChatRequest:
    kw.setdefault("model", "claude-sonnet-4-6")
    kw.setdefault("messages", [ChatMessage(role="user", content="weather in Tokyo?")])
    return ChatRequest(**kw)


# --- request side ------------------------------------------------------------


async def test_json_schema_becomes_a_forced_tool(monkeypatch) -> None:
    captured: dict = {}
    _fake_post(monkeypatch, captured, _reply([{"type": "text", "text": "{}"}]))
    await AnthropicProvider().chat(_req(response_format=JSON_SCHEMA_FORMAT), api_key="k")

    tools = captured["json"]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == _JSON_TOOL_NAME
    # The caller's schema is what constrains the tool input.
    assert tools[0]["input_schema"] == SCHEMA
    # Forced, not merely offered — otherwise the model may answer in prose.
    assert captured["json"]["tool_choice"] == {"type": "tool", "name": _JSON_TOOL_NAME}


async def test_json_object_forces_a_permissive_object_schema(monkeypatch) -> None:
    captured: dict = {}
    _fake_post(monkeypatch, captured, _reply([{"type": "text", "text": "{}"}]))
    await AnthropicProvider().chat(_req(response_format={"type": "json_object"}), api_key="k")
    # No schema was supplied, so any object satisfies the request.
    assert captured["json"]["tools"][0]["input_schema"] == {"type": "object"}
    assert captured["json"]["tool_choice"]["name"] == _JSON_TOOL_NAME


async def test_caller_tools_win_over_response_format(monkeypatch) -> None:
    # Only one tool can be forced. Honoring response_format here would silently
    # disable the tools the caller explicitly asked for, so the tools win and
    # the ambiguity is left with the caller.
    captured: dict = {}
    _fake_post(monkeypatch, captured, _reply([{"type": "text", "text": "hi"}]))
    await AnthropicProvider().chat(
        _req(tools=[WEATHER_TOOL], response_format=JSON_SCHEMA_FORMAT), api_key="k"
    )
    names = [t["name"] for t in captured["json"]["tools"]]
    assert names == ["get_weather"]
    assert _JSON_TOOL_NAME not in names


@pytest.mark.parametrize("rf", [None, {"type": "text"}, {"type": "future_mode"}])
async def test_no_tool_is_injected_without_a_json_format(monkeypatch, rf) -> None:
    captured: dict = {}
    _fake_post(monkeypatch, captured, _reply([{"type": "text", "text": "hi"}]))
    await AnthropicProvider().chat(_req(response_format=rf), api_key="k")
    assert "tools" not in captured["json"]
    assert "tool_choice" not in captured["json"]


# --- response side -----------------------------------------------------------


async def test_forced_tool_call_comes_back_as_json_content(monkeypatch) -> None:
    payload = {"city": "Tokyo", "temp_c": 21}
    _fake_post(
        monkeypatch,
        {},
        _reply([{"type": "tool_use", "id": "tu_1", "name": _JSON_TOOL_NAME, "input": payload}]),
    )
    result = await AnthropicProvider().chat(_req(response_format=JSON_SCHEMA_FORMAT), api_key="k")

    # The caller asked for JSON, not a tool call: content parses, and no
    # tool_calls leak into a response the caller never asked to be one.
    assert json.loads(result.content) == payload
    assert result.tool_calls is None


async def test_missing_tool_block_falls_back_to_text(monkeypatch) -> None:
    # The tool is forced, so this is the abnormal path — returning whatever text
    # did come back beats returning an empty response.
    _fake_post(monkeypatch, {}, _reply([{"type": "text", "text": "sorry, no"}]))
    result = await AnthropicProvider().chat(_req(response_format=JSON_SCHEMA_FORMAT), api_key="k")
    assert result.content == "sorry, no"


async def test_ordinary_tool_calls_are_unaffected(monkeypatch) -> None:
    # No response_format -> a real tool call must still surface as a tool call.
    _fake_post(
        monkeypatch,
        {},
        _reply([{"type": "tool_use", "id": "tu_9", "name": "get_weather", "input": {"city": "X"}}]),
    )
    result = await AnthropicProvider().chat(_req(tools=[WEATHER_TOOL]), api_key="k")
    assert result.tool_calls is not None
    assert result.tool_calls[0]["function"]["name"] == "get_weather"


# --- streaming ---------------------------------------------------------------
# With the tool forced, Anthropic streams the answer as input_json_delta
# fragments rather than text_delta. Those fragments *are* the JSON the caller
# asked for, so they go out as text deltas — which is what OpenAI's JSON-mode
# streaming looks like — instead of accumulating into a tool call.


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

    async def aread(self) -> bytes:  # pragma: no cover - only on the error path
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


_TOOL_STREAM = [
    'data: {"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":0}}}',
    'data: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"tool_use","id":"tu_1","name":"json_response"}}',
    'data: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"input_json_delta","partial_json":"{\\"city\\":"}}',
    'data: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"input_json_delta","partial_json":"\\"Tokyo\\"}"}}',
    'data: {"type":"message_delta","usage":{"output_tokens":5}}',
]


async def test_emulated_json_streams_as_text_deltas(monkeypatch) -> None:
    _fake_stream(monkeypatch, _TOOL_STREAM)
    deltas, tool_calls = [], []
    async for ev in AnthropicProvider().stream_events(
        _req(response_format=JSON_SCHEMA_FORMAT), api_key="k"
    ):
        if ev.delta:
            deltas.append(ev.delta)
        if ev.tool_calls:
            tool_calls.append(ev.tool_calls)

    assert json.loads("".join(deltas)) == {"city": "Tokyo"}
    # The caller asked for JSON content, so no tool call is surfaced.
    assert tool_calls == []


async def test_real_tool_stream_is_unaffected(monkeypatch) -> None:
    # Same wire events, but no response_format -> this is a genuine tool call
    # and must still be assembled as one rather than leaking as text.
    _fake_stream(monkeypatch, _TOOL_STREAM)
    deltas, tool_calls = [], []
    async for ev in AnthropicProvider().stream_events(_req(tools=[WEATHER_TOOL]), api_key="k"):
        if ev.delta:
            deltas.append(ev.delta)
        if ev.tool_calls:
            tool_calls.extend(ev.tool_calls)

    assert deltas == []
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"city": "Tokyo"}
