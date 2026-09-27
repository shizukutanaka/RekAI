"""`logprobs`/`top_logprobs` reach OpenAI-compatible providers and come back.

Per-token log probabilities power evals and confidence scoring. The compat
layer tolerated both fields via ``extra="allow"`` and silently dropped them, so
a caller asking for logprobs got a response with none. OpenAI-compatible
providers support them; the response rides ``choices[].logprobs`` — a sibling
of ``message``, not nested inside it.
"""

from __future__ import annotations

import httpx

from rekai.cache import cache_key, semantic_bucket
from rekai.openai_compat import chunk_delta, to_chat_completion, to_chat_request
from rekai.providers.openai import OpenAIProvider, _parse_openai_sse_event
from rekai.schemas import ChatCompletionsRequest, ChatMessage, ChatRequest, ChatResponse, Usage


def _req(**kw: object) -> ChatRequest:
    kw.setdefault("model", "m")
    kw.setdefault("messages", [ChatMessage(role="user", content="write an essay")])
    return ChatRequest(**kw)  # type: ignore[arg-type]


_LOGPROBS = {
    "content": [{"token": "hi", "logprob": -0.1, "top_logprobs": []}],
}


class _Resp:
    status_code = 200
    headers: dict = {}
    text = ""

    def json(self) -> dict:
        return {
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hi"},
                    "logprobs": _LOGPROBS,
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


class _Client:
    captured: dict = {}

    def __init__(self, *a: object, **k: object) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a: object):
        return False

    async def aclose(self) -> None:
        return None

    async def post(self, url, json=None, headers=None, **kw):
        _Client.captured = json or {}
        return _Resp()


async def test_logprobs_reach_openai(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(logprobs=True, top_logprobs=5), api_key="sk-x")
    assert _Client.captured["logprobs"] is True
    assert _Client.captured["top_logprobs"] == 5


async def test_no_logprobs_sends_no_key(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(), api_key="sk-x")
    assert "logprobs" not in _Client.captured
    assert "top_logprobs" not in _Client.captured


async def test_logprobs_parsed_from_response(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    result = await OpenAIProvider().chat(_req(logprobs=True), api_key="sk-x")
    assert result.logprobs == _LOGPROBS


def test_sse_logprobs_ride_the_delta_event() -> None:
    """Streaming logprobs arrive per-chunk at ``choices[0].logprobs``."""
    line = (
        'data: {"choices":[{"index":0,"delta":{"content":"hi"},'
        '"logprobs":{"content":[{"token":"hi","logprob":-0.1}]}}]}\n'
    )
    ev = _parse_openai_sse_event(line)
    assert ev is not None and ev.delta == "hi" and ev.logprobs is not None


def test_compat_maps_logprobs() -> None:
    req = ChatCompletionsRequest(
        model="gpt-5",
        messages=[{"role": "user", "content": "hi"}],
        logprobs=True,
        top_logprobs=3,
    )
    chat = to_chat_request(req)
    assert chat.logprobs is True and chat.top_logprobs == 3


def test_compat_response_places_logprobs_beside_message() -> None:
    resp = ChatResponse(
        id="c",
        provider="openai",
        model="gpt-5",
        content="hi",
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        cost_usd=None,
        cached=False,
        fallback_used=False,
        logprobs=_LOGPROBS,
        created=0,
    )
    choice = to_chat_completion(resp).choices[0]
    assert choice.logprobs == _LOGPROBS
    assert choice.message.content == "hi"


def test_chunk_delta_carries_logprobs() -> None:
    chunk = chunk_delta("c", 0, "m", "hi", logprobs=_LOGPROBS)
    assert chunk["choices"][0]["logprobs"] == _LOGPROBS
    # A logprobs-only chunk emits an empty delta rather than dropping data.
    chunk = chunk_delta("c", 0, "m", "", logprobs=_LOGPROBS)
    assert chunk["choices"][0]["delta"] == {}


def test_logprobs_change_the_cache_key() -> None:
    assert cache_key(_req(), "openai") != cache_key(_req(logprobs=True), "openai")
    assert cache_key(_req(logprobs=True), "openai") != cache_key(
        _req(logprobs=True, top_logprobs=5), "openai"
    )


def test_logprobs_change_the_semantic_bucket() -> None:
    a = semantic_bucket(_req(), "openai", "client-1")
    b = semantic_bucket(_req(logprobs=True), "openai", "client-1")
    assert a != b
