"""OpenAI's response-side identifiers end-to-end: system_fingerprint + service_tier.

OpenAI stamps both on every response — `system_fingerprint` identifies the
backend configuration that served the call (the debugging companion to `seed`),
and `service_tier` reports which tier actually handled it when the request said
"auto". They were dropped before; now they ride ProviderResult/StreamEvent into
the native ChatResponse, the native SSE summary, the compat response, and the
compat stream chunks. Anthropic also reports `service_tier` in its usage
object ("auto"/"standard_only" — a billing-tier echo, accepted on requests
too); Gemini/Ollama have no equivalent and report None.
"""

from __future__ import annotations

import httpx

from rekai.openai_compat import chunk_delta, chunk_usage, to_chat_completion
from rekai.providers.openai import OpenAIProvider, _parse_openai_sse_event
from rekai.schemas import ChatMessage, ChatRequest, ChatResponse, Usage


def _req() -> ChatRequest:
    return ChatRequest(model="m", messages=[ChatMessage(role="user", content="hi")])


def _fake_client(monkeypatch, payload: dict) -> None:
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


# --- OpenAI parsing ---------------------------------------------------------


async def test_chat_parses_fingerprint_and_tier(monkeypatch) -> None:
    _fake_client(
        monkeypatch,
        {
            "model": "m",
            "system_fingerprint": "fp_abc123",
            "service_tier": "flex",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )
    result = await OpenAIProvider().chat(_req(), api_key="sk-x")
    assert result.system_fingerprint == "fp_abc123"
    assert result.service_tier == "flex"


async def test_chat_defaults_to_none(monkeypatch) -> None:
    _fake_client(
        monkeypatch,
        {
            "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )
    result = await OpenAIProvider().chat(_req(), api_key="sk-x")
    assert result.system_fingerprint is None
    assert result.service_tier is None


def test_stream_metadata_only_chunk() -> None:
    # The role-announcement chunk carries fp/tier but no delta.
    line = (
        'data: {"system_fingerprint": "fp_abc", "service_tier": "default", '
        '"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": null}]}'
    )
    event = _parse_openai_sse_event(line)
    assert event is not None
    assert event.delta is None and event.usage is None
    assert event.system_fingerprint == "fp_abc"
    assert event.service_tier == "default"


def test_stream_fp_on_delta_and_usage_chunks() -> None:
    delta_event = _parse_openai_sse_event(
        'data: {"system_fingerprint": "fp_x", "choices": [{"index": 0, '
        '"delta": {"content": "hi"}, "finish_reason": null}]}'
    )
    assert delta_event is not None and delta_event.system_fingerprint == "fp_x"
    usage_event = _parse_openai_sse_event(
        'data: {"system_fingerprint": "fp_y", "service_tier": "flex", "choices": [], '
        '"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}'
    )
    assert usage_event is not None and usage_event.usage is not None
    assert usage_event.system_fingerprint == "fp_y"
    assert usage_event.service_tier == "flex"


# --- emission ---------------------------------------------------------------


def _resp(fp: str | None, tier: str | None) -> ChatResponse:
    return ChatResponse(
        id="chatcmpl-x",
        provider="openai",
        model="m",
        content="ok",
        created=0,
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        system_fingerprint=fp,
        service_tier=tier,
    )


def test_compat_completion_carries_both() -> None:
    completion = to_chat_completion(_resp("fp_abc", "flex"))
    assert completion.system_fingerprint == "fp_abc"
    assert completion.service_tier == "flex"


def test_compat_completion_nulls_when_absent() -> None:
    completion = to_chat_completion(_resp(None, None))
    assert completion.system_fingerprint is None
    assert completion.service_tier is None


def test_stream_chunks_stamped_with_both() -> None:
    chunk = chunk_delta("c", 0, "m", "hi", system_fingerprint="fp_a", service_tier="flex")
    assert chunk["system_fingerprint"] == "fp_a"
    assert chunk["service_tier"] == "flex"
    usage_chunk = chunk_usage("c", 0, "m", Usage(), system_fingerprint="fp_a", service_tier="flex")
    assert usage_chunk["system_fingerprint"] == "fp_a"
    assert usage_chunk["service_tier"] == "flex"


def test_stream_chunks_omit_when_unknown() -> None:
    chunk = chunk_delta("c", 0, "m", "hi")
    assert "system_fingerprint" not in chunk
    assert "service_tier" not in chunk


# --- Anthropic service_tier (usage.service_tier, billing-tier echo) ---------


async def test_anthropic_service_tier_round_trip(monkeypatch) -> None:
    from rekai.providers.anthropic import AnthropicProvider

    captured: dict = {}

    class FakeResponse:
        status_code = 200
        headers: dict = {}

        def json(self) -> dict:
            return {
                "model": "claude-x",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 2,
                    "service_tier": "standard",
                },
            }

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None, **kw):
            captured.update(json or {})
            return FakeResponse()

        async def aclose(self):
            return None

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    req = ChatRequest(
        model="claude-x",
        messages=[ChatMessage(role="user", content="hi")],
        service_tier="standard_only",
    )
    result = await AnthropicProvider().chat(req, api_key="sk-x")
    # Request side forwarded verbatim; response side echoes what billed.
    assert captured["service_tier"] == "standard_only"
    assert result.service_tier == "standard"
