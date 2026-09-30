"""Web-search citations (OpenAI `message.annotations` / streamed
`delta.annotations`) pass through verbatim instead of being dropped — a caller
who paid for web search should be able to see what the model cited."""

from __future__ import annotations

import httpx

from rekai import anthropic_compat, openai_compat
from rekai.providers.openai import OpenAIProvider
from rekai.schemas import ChatMessage, ChatRequest, ChatResponse, Usage

_CITATION = {
    "type": "url_citation",
    "url_citation": {"url": "https://ex.com/a", "title": "A", "start_index": 0, "end_index": 3},
}


def _req() -> ChatRequest:
    return ChatRequest(
        model="gpt-4o-search-preview",
        messages=[ChatMessage(role="user", content="hi")],
    )


class _CitationClient:
    def __init__(self, *a, **k) -> None:
        pass

    async def aclose(self) -> None:
        return None

    async def post(self, url, json=None, headers=None):
        class _Resp:
            status_code = 200

            def json(self):
                return {
                    "id": "x",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "ans",
                                "annotations": [_CITATION],
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }

        return _Resp()


class _CitationStreamClient:
    def __init__(self, *a, **k) -> None:
        pass

    async def aclose(self) -> None:
        return None

    def stream(self, method, url, json=None, headers=None):
        class _Resp:
            status_code = 200

            async def aiter_lines(self):
                yield 'data: {"choices":[{"index":0,"delta":{"content":"ans"}}]}'
                yield (
                    'data: {"choices":[{"index":0,"delta":{"annotations":'
                    '[{"type":"url_citation","url_citation":{"url":"https://ex.com/a","title":"A",'
                    '"start_index":0,"end_index":3}}]}}]}'
                )
                yield 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}'

            async def aread(self) -> bytes:
                return b""

        class _Ctx:
            async def __aenter__(self):
                return _Resp()

            async def __aexit__(self, *a):
                return False

        return _Ctx()


async def test_openai_chat_surfaces_annotations(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _CitationClient)
    result = await OpenAIProvider().chat(_req(), "sk-x")
    assert result.content == "ans"
    assert result.annotations == [_CITATION]


async def test_openai_stream_surfaces_annotation_chunks(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _CitationStreamClient)
    events = [ev async for ev in OpenAIProvider().stream_events(_req(), "sk-x")]
    assert [e.delta for e in events if e.delta] == ["ans"]
    anns = [a for e in events if e.annotations for a in e.annotations]
    assert anns == [_CITATION]


def _resp() -> ChatResponse:
    return ChatResponse(
        id="r1",
        provider="openai",
        model="gpt-4o-search-preview",
        content="ans",
        annotations=[_CITATION],
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        created=1,
    )


def test_openai_compat_emits_message_annotations() -> None:
    msg = openai_compat.to_chat_completion(_resp()).choices[0].message
    assert msg.annotations == [_CITATION]


def test_anthropic_compat_passes_annotations_as_extra() -> None:
    # Anthropic's own citations schema needs cited_text RekAI doesn't have —
    # the raw OpenAI annotations ride as an observability extra instead.
    msg = anthropic_compat.to_message(_resp())
    assert msg["annotations"] == [_CITATION]
    assert msg["content"] == [{"type": "text", "text": "ans"}]


def test_openai_stream_chunk_annotations_shape() -> None:
    chunk = openai_compat.chunk_annotations("c", 1, "m", [_CITATION])
    assert chunk["choices"][0]["delta"] == {"annotations": [_CITATION]}
