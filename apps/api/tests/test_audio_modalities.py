"""`modalities`/`audio` reach OpenAI-compatible providers and come back.

OpenAI's audio-capable models (gpt-4o-audio-preview) need
``modalities: ["text","audio"]`` plus an ``audio`` config ({voice, format}),
and return the clip at ``choices[].message.audio``. The compat layer tolerated
both fields via ``extra="allow"`` and dropped them, so a caller asking for
audio always got a text-only response.
"""

from __future__ import annotations

import httpx

from rekai.cache import cache_key, semantic_bucket
from rekai.openai_compat import chunk_audio, to_chat_completion, to_chat_request
from rekai.providers.openai import OpenAIProvider, _parse_openai_sse_event
from rekai.schemas import ChatCompletionsRequest, ChatMessage, ChatRequest, ChatResponse, Usage

_AUDIO_CFG = {"voice": "alloy", "format": "wav"}
_AUDIO_OUT = {"id": "a1", "data": "UklGRg==", "transcript": "hi", "expires_at": 0}


def _req(**kw: object) -> ChatRequest:
    kw.setdefault("model", "gpt-4o-audio-preview")
    kw.setdefault("messages", [ChatMessage(role="user", content="say hi")])
    return ChatRequest(**kw)  # type: ignore[arg-type]


class _Resp:
    status_code = 200
    headers: dict = {}
    text = ""

    def json(self) -> dict:
        return {
            "model": "gpt-4o-audio-preview",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "audio": _AUDIO_OUT,
                    },
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


async def test_modalities_and_audio_reach_openai(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(
        _req(modalities=["text", "audio"], audio=_AUDIO_CFG), api_key="sk-x"
    )
    assert _Client.captured["modalities"] == ["text", "audio"]
    assert _Client.captured["audio"] == _AUDIO_CFG


async def test_no_audio_sends_no_keys(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(), api_key="sk-x")
    assert "modalities" not in _Client.captured
    assert "audio" not in _Client.captured


async def test_audio_parsed_from_message(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    result = await OpenAIProvider().chat(_req(), api_key="sk-x")
    assert result.audio == _AUDIO_OUT


def test_sse_audio_is_a_separate_event() -> None:
    """A ``delta.audio`` chunk must not land in the text delta — it is base64
    audio, not prose, and appending it would corrupt the completion."""
    line = 'data: {"choices":[{"index":0,"delta":{"audio":{"data":"UklGRg=="}}}]}\n'
    ev = _parse_openai_sse_event(line)
    assert ev is not None and ev.delta is None and ev.audio == {"data": "UklGRg=="}


def test_compat_maps_modalities_and_audio() -> None:
    req = ChatCompletionsRequest(
        model="gpt-4o-audio-preview",
        messages=[{"role": "user", "content": "hi"}],
        modalities=["text", "audio"],
        audio=_AUDIO_CFG,
    )
    chat = to_chat_request(req)
    assert chat.modalities == ["text", "audio"]
    assert chat.audio == _AUDIO_CFG


def test_compat_response_places_audio_on_the_message() -> None:
    resp = ChatResponse(
        id="c",
        provider="openai",
        model="gpt-4o-audio-preview",
        content="",
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        cost_usd=None,
        cached=False,
        fallback_used=False,
        audio=_AUDIO_OUT,
        created=0,
    )
    assert to_chat_completion(resp).choices[0].message.audio == _AUDIO_OUT


def test_chunk_audio_shape() -> None:
    chunk = chunk_audio("c", 0, "m", _AUDIO_OUT)
    assert chunk["choices"][0]["delta"]["audio"] == _AUDIO_OUT
    assert chunk["choices"][0]["finish_reason"] is None


def test_audio_request_changes_the_cache_key() -> None:
    a = cache_key(_req(), "openai")
    b = cache_key(_req(modalities=["text", "audio"], audio=_AUDIO_CFG), "openai")
    assert a != b


def test_audio_request_changes_the_semantic_bucket() -> None:
    a = semantic_bucket(_req(), "openai", "client-1")
    b = semantic_bucket(_req(audio=_AUDIO_CFG), "openai", "client-1")
    assert a != b
