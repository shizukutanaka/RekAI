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
