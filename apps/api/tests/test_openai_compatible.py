"""Tests for the configurable OpenAI-compatible provider."""

from __future__ import annotations

import httpx
import pytest

from rekai.config import Settings
from rekai.providers.base import ProviderError
from rekai.providers.openai_compatible import OpenAICompatibleProvider
from rekai.schemas import ChatMessage, ChatRequest


def _req(model: str = "llama-3.1-70b") -> ChatRequest:
    return ChatRequest(model=model, messages=[ChatMessage(role="user", content="hi")])


def test_custom_model_list_parsing() -> None:
    s = Settings(custom_models="a, b ,, c")
    assert s.custom_model_list == ["a", "b", "c"]


def test_server_key_configured_reflects_key() -> None:
    assert OpenAICompatibleProvider("groq", "https://x/v1", api_key="k").server_key_configured()
    assert not OpenAICompatibleProvider("groq", "https://x/v1").server_key_configured()


async def test_list_models_returns_configured() -> None:
    p = OpenAICompatibleProvider("groq", "https://x/v1", models=["m1", "m2"])
    assert await p.list_models(None) == ["m1", "m2"]


async def test_list_embedding_models_returns_configured() -> None:
    p = OpenAICompatibleProvider(
        "vllm", "https://x/v1", models=["chat-1"], embedding_models=["embed-1", "embed-2"]
    )
    assert await p.list_embedding_models(None) == ["embed-1", "embed-2"]
    # Without configuration, custom backends advertise no embedding models.
    assert await OpenAICompatibleProvider("x", "https://x/v1").list_embedding_models(None) == []


async def test_requires_key_uses_provider_name() -> None:
    p = OpenAICompatibleProvider("groq", "https://x/v1", api_key=None)
    with pytest.raises(ProviderError) as exc:
        await p.chat(_req(), api_key=None)
    assert exc.value.status_code == 401
    assert "groq" in str(exc.value)
    assert "REKAI_CUSTOM_API_KEY" in str(exc.value)


async def test_chat_uses_custom_base_url_and_key(monkeypatch) -> None:
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {
                "model": "llama-3.1-70b",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
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
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    p = OpenAICompatibleProvider("groq", "https://api.groq.com/openai/v1", api_key="gk")
    result = await p.chat(_req(), api_key=None)
    assert result.content == "ok"
    assert captured["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer gk"
