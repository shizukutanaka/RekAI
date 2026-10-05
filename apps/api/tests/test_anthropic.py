"""Tests for the Anthropic provider, with the HTTP layer mocked."""

from __future__ import annotations

import httpx
import pytest

from rekai.providers.anthropic import AnthropicProvider
from rekai.providers.base import ProviderError
from rekai.schemas import ChatMessage, ChatRequest


def _req(**kwargs) -> ChatRequest:
    kwargs.setdefault("model", "claude-sonnet-4-6")
    kwargs.setdefault("messages", [ChatMessage(role="user", content="hi")])
    return ChatRequest(**kwargs)


async def test_requires_key() -> None:
    with pytest.raises(ProviderError) as exc:
        await AnthropicProvider().chat(_req(), api_key=None)
    assert exc.value.status_code == 401


async def test_chat_parses_response(monkeypatch) -> None:
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "Hello there"}],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            }

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    msgs = [
        ChatMessage(role="system", content="be brief"),
        ChatMessage(role="user", content="hi"),
    ]
    result = await AnthropicProvider().chat(
        _req(messages=msgs, max_tokens=64), api_key="sk-ant-test"
    )

    assert result.content == "Hello there"
    assert result.usage.total_tokens == 7
    # System prompt is hoisted out of `messages`.
    assert captured["json"]["system"] == "be brief"
    assert all(m["role"] != "system" for m in captured["json"]["messages"])
    assert captured["json"]["max_tokens"] == 64
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert "anthropic-version" in captured["headers"]


async def test_chat_propagates_http_error(monkeypatch) -> None:
    class FakeResponse:
        status_code = 400
        text = "bad request"
        headers: dict = {}

        def json(self) -> dict:  # pragma: no cover - not reached
            return {}

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    with pytest.raises(ProviderError) as exc:
        await AnthropicProvider().chat(_req(), api_key="sk-ant-test")
    assert exc.value.status_code == 400


# --- error paths, stream edges, and resilient parsing -------------------------
#
# The branches a client actually sees when Anthropic errors, the daemon
# answer is corrupt, or the reply uses less-common block types.


async def test_chat_rejects_system_only_messages() -> None:
    """System turns hoist out of `messages`; a request holding only system
    content must fail fast instead of POSTing an empty messages array."""
    with pytest.raises(ProviderError) as exc:
        await AnthropicProvider().chat(
            _req(messages=[ChatMessage(role="system", content="be brief")]),
            api_key="sk-ant-test",
        )
    assert exc.value.status_code == 400


async def test_chat_transport_failure_is_wrapped(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def post(self, *a, **k):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    with pytest.raises(ProviderError):
        await AnthropicProvider().chat(_req(), api_key="sk-ant-test")


def _capture_client(captured: dict):
    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def post(self, url, json, headers):
            captured["json"] = json
            return FakeResponse()

    return FakeClient


async def test_json_object_forces_an_any_object_tool(monkeypatch) -> None:
    """json_object carries no schema, so the forced-tool emulation asks for
    any object — input_schema {"type": "object"}."""
    captured: dict = {}
    monkeypatch.setattr(httpx, "AsyncClient", _capture_client(captured))
    await AnthropicProvider().chat(
        _req(response_format={"type": "json_object"}), api_key="sk-ant-test"
    )
    assert captured["json"]["tools"][0]["input_schema"] == {"type": "object"}
    assert captured["json"]["tool_choice"] == {"type": "tool", "name": "json_response"}


async def test_tool_choice_dict_maps_to_named_tool(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(httpx, "AsyncClient", _capture_client(captured))
    await AnthropicProvider().chat(
        _req(
            tools=[
                {
                    "type": "function",
                    "function": {"name": "get_weather", "parameters": {"type": "object"}},
                }
            ],
            tool_choice={"type": "function", "function": {"name": "get_weather"}},
        ),
        api_key="sk-ant-test",
    )
    assert captured["json"]["tool_choice"] == {"type": "tool", "name": "get_weather"}


async def test_stream_wrapper_yields_text_deltas(monkeypatch) -> None:
    from tests.test_streaming import _FakeClient

    lines = [
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"message_stop"}',
    ]
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(lines))
    req = _req()
    assert [d async for d in AnthropicProvider().stream(req, api_key="sk-ant")] == ["hi"]


async def test_stream_upstream_error_status_propagates(monkeypatch) -> None:
    from tests.test_streaming import _FakeClient

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient([], status_code=429))
    with pytest.raises(ProviderError) as exc:
        async for _ in AnthropicProvider().stream_events(_req(), api_key="sk-ant"):
            pass
    assert exc.value.status_code == 429
    assert "error body" in str(exc.value)


async def test_stream_transport_failure_is_wrapped(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        def stream(self, *a, **k):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    with pytest.raises(ProviderError):
        async for _ in AnthropicProvider().stream_events(_req(), api_key="sk-ant"):
            pass


async def test_stream_skips_blank_corrupt_and_non_data_lines(monkeypatch) -> None:
    """Keep-alives and truncated chunks must not abort the stream — the valid
    events around them still parse."""
    from tests.test_streaming import _FakeClient

    lines = [
        ": keep-alive",
        "data:",
        'data: {"type":"message_start","message":{"usage":{"input_tokens":1}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"a"}}',
        'data: {"type":"message_stop"}',
    ]
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(lines))
    events = [e async for e in AnthropicProvider().stream_events(_req(), api_key="sk-ant")]
    assert any(e.delta == "a" for e in events)


async def test_stream_redacted_thinking_arrives_whole(monkeypatch) -> None:
    """redacted_thinking has no deltas — it must emit as a thinking_block at
    block-start so the caller can echo it back verbatim."""
    from tests.test_streaming import _FakeClient

    lines = [
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"redacted_thinking","data":"enc"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"message_stop"}',
    ]
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(lines))
    events = [e async for e in AnthropicProvider().stream_events(_req(), api_key="sk-ant")]
    blocks = [e.thinking_block for e in events if e.thinking_block is not None]
    assert blocks == [{"type": "redacted_thinking", "data": "enc"}]


async def test_stream_extra_block_citations_merge_into_block(monkeypatch) -> None:
    """A citations_delta landing on a server-tool block joins that block's
    citations list — the completed extra_block matches what upstream sent."""
    from tests.test_streaming import _FakeClient

    lines = [
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"web_search_tool_result","tool_use_id":"x","content":[]}}',
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"citations_delta","citation":{"url":"https://a.example"}}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"message_stop"}',
    ]
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(lines))
    events = [e async for e in AnthropicProvider().stream_events(_req(), api_key="sk-ant")]
    block = next(e.extra_block for e in events if e.extra_block is not None)
    assert block["citations"] == [{"url": "https://a.example"}]


async def test_stream_extra_block_unparseable_input_stays_raw(monkeypatch) -> None:
    """When the accumulated input_json isn't valid JSON at block_stop the raw
    string is kept — losing it entirely would corrupt the tool trace."""
    from tests.test_streaming import _FakeClient

    lines = [
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"server_tool_use","id":"s1","name":"web_search"}}',
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"input_json_delta","partial_json":"{not json"}}',
        'data: {"type":"content_block_stop","index":0}',
        'data: {"type":"message_stop"}',
    ]
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(lines))
    events = [e async for e in AnthropicProvider().stream_events(_req(), api_key="sk-ant")]
    block = next(e.extra_block for e in events if e.extra_block is not None)
    assert block["input"] == "{not json"


def test_translate_assistant_message_orders_blocks_and_tolerates_bad_args() -> None:
    """Assistant replay: thinking first, then server-tool trace, then text,
    then tool_use — Anthropic's required order. A tool_call whose arguments
    aren't JSON is sent as an empty input rather than crashing the request."""
    from rekai.providers.anthropic import _translate_messages

    out = _translate_messages(
        [
            ChatMessage(
                role="assistant",
                content="the answer",
                thinking_blocks=[{"type": "thinking", "thinking": "hmm"}],
                extra_blocks=[{"type": "server_tool_use", "id": "s1"}],
                tool_calls=[
                    {
                        "id": "tu_1",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{bad json"},
                    }
                ],
            )
        ]
    )
    assert [b["type"] for b in out[0]["content"]] == [
        "thinking",
        "server_tool_use",
        "text",
        "tool_use",
    ]
    assert out[0]["content"][3]["input"] == {}


async def test_json_schema_without_a_schema_still_forces_json(monkeypatch) -> None:
    """A json_schema request with no schema body still means "JSON, please" —
    the forced tool asks for any object rather than dropping the intent."""
    captured: dict = {}
    monkeypatch.setattr(httpx, "AsyncClient", _capture_client(captured))
    await AnthropicProvider().chat(
        _req(response_format={"type": "json_schema"}), api_key="sk-ant-test"
    )
    assert captured["json"]["tools"][0]["input_schema"] == {"type": "object"}
    assert captured["json"]["tool_choice"] == {"type": "tool", "name": "json_response"}


async def test_tool_choice_dict_without_a_name_sends_nothing(monkeypatch) -> None:
    """A dict tool_choice with no function name can't name a tool — sending
    Anthropic a nameless tool_choice would be an upstream 400."""
    captured: dict = {}
    monkeypatch.setattr(httpx, "AsyncClient", _capture_client(captured))
    await AnthropicProvider().chat(
        _req(
            tools=[
                {
                    "type": "function",
                    "function": {"name": "get_weather", "parameters": {"type": "object"}},
                }
            ],
            tool_choice={"type": "allowed_tools", "mode": "auto"},
        ),
        api_key="sk-ant-test",
    )
    assert "tool_choice" not in captured["json"]
