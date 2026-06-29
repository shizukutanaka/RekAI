"""Tests for the Gemini provider, with the HTTP layer mocked."""

from __future__ import annotations

import httpx
import pytest

from rekai.providers.base import ProviderError
from rekai.providers.gemini import GeminiProvider, _parse_gemini_sse_line
from rekai.schemas import ChatMessage, ChatRequest


def _req(**kwargs) -> ChatRequest:
    kwargs.setdefault("model", "gemini-1.5-flash")
    kwargs.setdefault("messages", [ChatMessage(role="user", content="hi")])
    return ChatRequest(**kwargs)


async def test_requires_key() -> None:
    with pytest.raises(ProviderError) as exc:
        await GeminiProvider().chat(_req(), api_key=None)
    assert exc.value.status_code == 401


async def test_chat_parses_response(monkeypatch) -> None:
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {
                "candidates": [{"content": {"parts": [{"text": "Hello "}, {"text": "world"}]}}],
                "usageMetadata": {
                    "promptTokenCount": 3,
                    "candidatesTokenCount": 2,
                    "totalTokenCount": 5,
                },
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
        ChatMessage(role="system", content="be terse"),
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="prev"),
    ]
    result = await GeminiProvider().chat(_req(messages=msgs, max_tokens=32), api_key="g-key")

    assert result.content == "Hello world"
    assert result.usage.total_tokens == 5
    # system prompt hoisted, assistant mapped to role "model".
    assert captured["json"]["systemInstruction"]["parts"][0]["text"] == "be terse"
    roles = [c["role"] for c in captured["json"]["contents"]]
    assert roles == ["user", "model"]
    assert captured["json"]["generationConfig"]["maxOutputTokens"] == 32
    assert captured["headers"]["x-goog-api-key"] == "g-key"
    assert ":generateContent" in captured["url"]


async def test_chat_propagates_http_error(monkeypatch) -> None:
    class FakeResponse:
        status_code = 429
        text = "rate limited"

        def json(self) -> dict:  # pragma: no cover
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
        await GeminiProvider().chat(_req(), api_key="g-key")
    assert exc.value.status_code == 429


@pytest.mark.parametrize(
    "line,expected",
    [
        ('data: {"candidates":[{"content":{"parts":[{"text":"Hi"}]}}]}', "Hi"),
        ('data: {"candidates":[]}', None),
        ("", None),
        ("event: noise", None),
    ],
)
def test_gemini_sse_parser(line, expected) -> None:
    assert _parse_gemini_sse_line(line) == expected
