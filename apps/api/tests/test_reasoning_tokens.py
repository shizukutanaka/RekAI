"""Reasoning/thinking token accounting end-to-end.

Reasoning models (OpenAI o-series and gpt-5, Gemini thinking models) bill a
separate slice of completion tokens for chain-of-thought. Providers report it
under different names — ``usage.completion_tokens_details.reasoning_tokens``
(OpenAI) and ``usageMetadata.thoughtsTokenCount`` (Gemini) — and RekAI surfaces
it as the flat ``usage.reasoning_tokens`` on the native surface and re-nests it
on the OpenAI-compat surface for SDK parity.
"""

from __future__ import annotations

import httpx

from rekai.openai_compat import chunk_usage, to_chat_completion
from rekai.providers.gemini import GeminiProvider
from rekai.providers.openai import OpenAIProvider, _parse_openai_sse_event
from rekai.schemas import ChatMessage, ChatRequest, ChatResponse, Usage


def _req(**kw: object) -> ChatRequest:
    kw.setdefault("model", "m")
    kw.setdefault("messages", [ChatMessage(role="user", content="hi")])
    return ChatRequest(**kw)  # type: ignore[arg-type]


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


_OPENAI_REPLY = {
    "model": "o4-mini",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
    "usage": {
        "prompt_tokens": 10,
        "completion_tokens": 50,
        "total_tokens": 60,
        "completion_tokens_details": {"reasoning_tokens": 40},
    },
}


# --- OpenAI ---------------------------------------------------------------


async def test_openai_parses_reasoning_tokens(monkeypatch) -> None:
    _fake_client(monkeypatch, _OPENAI_REPLY)
    result = await OpenAIProvider().chat(_req(), api_key="sk-x")
    assert result.usage.reasoning_tokens == 40
    # A breakdown of completion_tokens, not additive.
    assert result.usage.completion_tokens == 50


async def test_openai_missing_details_defaults_to_zero(monkeypatch) -> None:
    payload = {
        "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    _fake_client(monkeypatch, payload)
    result = await OpenAIProvider().chat(_req(), api_key="sk-x")
    assert result.usage.reasoning_tokens == 0


def test_openai_stream_usage_reads_reasoning_tokens() -> None:
    line = (
        'data: {"usage": {"prompt_tokens": 10, "completion_tokens": 50, '
        '"total_tokens": 60, "completion_tokens_details": {"reasoning_tokens": 40}}}'
    )
    event = _parse_openai_sse_event(line)
    assert event is not None and event.usage is not None
    assert event.usage.reasoning_tokens == 40


# --- Gemini ---------------------------------------------------------------


async def test_gemini_parses_thoughts_token_count(monkeypatch) -> None:
    _fake_client(
        monkeypatch,
        {
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            "usageMetadata": {
                "promptTokenCount": 3,
                "candidatesTokenCount": 20,
                "totalTokenCount": 23,
                "thoughtsTokenCount": 15,
            },
        },
    )
    result = await GeminiProvider().chat(_req(), api_key="g-key")
    assert result.usage.reasoning_tokens == 15


# --- compat surface --------------------------------------------------------


def _resp(reasoning: int) -> ChatResponse:
    return ChatResponse(
        id="chatcmpl-x",
        provider="openai",
        model="o4-mini",
        content="ok",
        created=0,
        usage=Usage(
            prompt_tokens=10,
            completion_tokens=50,
            total_tokens=60,
            reasoning_tokens=reasoning,
        ),
    )


def test_compat_completion_emits_nested_reasoning_tokens() -> None:
    completion = to_chat_completion(_resp(40))
    usage = completion.usage.model_dump()
    assert usage["reasoning_tokens"] == 40
    assert usage["completion_tokens_details"] == {"reasoning_tokens": 40}


def test_compat_completion_omits_details_when_zero() -> None:
    completion = to_chat_completion(_resp(0))
    assert completion.usage.completion_tokens_details is None


def test_stream_usage_chunk_emits_nested_reasoning_tokens() -> None:
    chunk = chunk_usage("c", 0, "m", _resp(40).usage)
    assert chunk["usage"]["completion_tokens_details"] == {"reasoning_tokens": 40}


def test_stream_usage_chunk_omits_details_when_zero() -> None:
    chunk = chunk_usage("c", 0, "m", _resp(0).usage)
    assert "completion_tokens_details" not in chunk["usage"]
