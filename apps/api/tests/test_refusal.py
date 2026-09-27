"""The model's refusal text (`message.refusal` / `delta.refusal`) rides the
pipeline as its own field instead of being flattened into or dropped from
`content` — an OpenAI refusal otherwise looked like an empty answer."""

from __future__ import annotations

import json

import httpx

from rekai import anthropic_compat, openai_compat
from rekai.providers.openai import OpenAIProvider
from rekai.schemas import ChatMessage, ChatRequest, ChatResponse, Usage


def _req() -> ChatRequest:
    return ChatRequest(model="gpt-4o", messages=[ChatMessage(role="user", content="hack pls")])


class _RefusalClient:
    def __init__(self, *a, **k) -> None:
        pass

    async def aclose(self) -> None:
        return None

    async def post(self, url, json=None, headers=None):
        class _Resp:
            status_code = 200

            def json(self):
                return {
                    "id": "chatcmpl-x",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "refusal": "I can't help with that.",
                            },
                            "finish_reason": "content_filter",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5},
                }

        return _Resp()


class _RefusalStreamClient:
    def __init__(self, *a, **k) -> None:
        pass

    async def aclose(self) -> None:
        return None

    def stream(self, method, url, json=None, headers=None):
        class _Resp:
            status_code = 200

            async def aiter_lines(self):
                yield 'data: {"choices":[{"index":0,"delta":{"refusal":"I can\'t "}}]}'
                yield 'data: {"choices":[{"index":0,"delta":{"refusal":"help."}}]}'
                yield 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"content_filter"}]}'
                yield 'data: {"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}'

            async def aread(self) -> bytes:
                return b""

        class _Ctx:
            async def __aenter__(self):
                return _Resp()

            async def __aexit__(self, *a):
                return False

        return _Ctx()


async def test_openai_chat_surfaces_message_refusal(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _RefusalClient)
    result = await OpenAIProvider().chat(_req(), "sk-x")
    assert result.content == ""
    assert result.refusal == "I can't help with that."
    assert result.finish_reason == "content_filter"


async def test_openai_stream_surfaces_refusal_deltas(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _RefusalStreamClient)
    events = [ev async for ev in OpenAIProvider().stream_events(_req(), "sk-x")]
    assert [e.refusal_delta for e in events if e.refusal_delta] == ["I can't ", "help."]
    assert any(e.finish_reason == "content_filter" for e in events)


def _resp(refusal: str | None) -> ChatResponse:
    return ChatResponse(
        id="r1",
        provider="openai",
        model="gpt-4o",
        content="" if refusal else "ok",
        refusal=refusal,
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        finish_reason="content_filter" if refusal else "stop",
        created=1,
    )


def test_openai_compat_emits_message_refusal() -> None:
    msg = openai_compat.to_chat_completion(_resp("no can do")).choices[0].message
    assert msg.refusal == "no can do"
    assert msg.content is None


def test_anthropic_compat_folds_refusal_into_text_block() -> None:
    # Anthropic has no refusal channel: the refusal text is the message content.
    msg = anthropic_compat.to_message(_resp("declined"))
    assert msg["content"] == [{"type": "text", "text": "declined"}]
    assert msg["stop_reason"] == "refusal"


def test_openai_stream_chunk_refusal_shape() -> None:
    chunk = openai_compat.chunk_refusal("c", 1, "m", "can't")
    data = chunk["choices"][0]["delta"]
    assert data == {"refusal": "can't"}
    # An SDK sees it byte-for-byte on the wire.
    assert json.loads(json.dumps(chunk))["choices"][0]["delta"]["refusal"] == "can't"
