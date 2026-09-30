"""Tests for the configurable OpenAI-compatible provider."""

from __future__ import annotations

import httpx

from rekai.config import Settings
from rekai.main import create_app
from rekai.providers.openai_compatible import OpenAICompatibleProvider
from rekai.providers.registry import configure_custom_provider
from rekai.schemas import ChatMessage, ChatRequest


def _req(model: str = "llama-3.1-70b") -> ChatRequest:
    return ChatRequest(model=model, messages=[ChatMessage(role="user", content="hi")])


def test_custom_model_list_parsing() -> None:
    s = Settings(custom_models="a, b ,, c")
    assert s.custom_model_list == ["a", "b", "c"]


def test_server_key_configured_is_true_without_a_key() -> None:
    # A custom backend is *ready* with or without a key: vLLM, LM Studio and
    # llama.cpp serve unauthenticated. This previously reported not-ready with no
    # key, which showed a healthy local backend as unusable on /health.
    assert OpenAICompatibleProvider("groq", "https://x/v1", api_key="k").server_key_configured()
    assert OpenAICompatibleProvider("vllm", "https://x/v1").server_key_configured()


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


async def test_keyless_backend_is_called_without_an_authorization_header(monkeypatch) -> None:
    # This test used to assert the opposite — that RekAI 401s locally when no key
    # is set. That refusal never let a request leave the process, which locked
    # the gateway out of exactly the keyless local servers this provider exists
    # to reach (README: "vLLM, LM Studio"). A backend that does need a key now
    # answers with its own 401 instead of RekAI guessing on its behalf.
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {
                "choices": [{"message": {"content": "ok", "role": "assistant"}}],
                "model": "local",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def post(self, url, json, headers):
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    provider = OpenAICompatibleProvider("vllm", "http://localhost:8000/v1", api_key=None)

    result = await provider.chat(_req(), api_key=None)

    assert result.content == "ok"
    assert "Authorization" not in captured["headers"]


async def test_byok_key_is_sent_even_when_none_is_configured(monkeypatch) -> None:
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {
                "choices": [{"message": {"content": "ok", "role": "assistant"}}],
                "model": "m",
                "usage": {},
            }

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def post(self, url, json, headers):
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    provider = OpenAICompatibleProvider("groq", "https://x/v1", api_key=None)

    await provider.chat(_req(), api_key="sk-byok")

    assert captured["headers"]["Authorization"] == "Bearer sk-byok"


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


def test_create_app_registers_custom_provider() -> None:
    # The registry wires a custom OpenAI-compatible backend from the app's
    # Settings — via create_app -> configure_custom_provider, not the process
    # env at import time (O-1). The other tests build the provider directly and
    # never exercise that settings -> registry path; this covers it.
    import rekai.providers.registry as registry

    create_app(
        Settings(
            environment="test",
            default_provider="echo",
            custom_base_url="https://llm.example.com/v1",
            custom_name="myllm",
            custom_api_key="sk-custom",
            custom_models="my-model-a, my-model-b",
        )
    )
    try:
        provider = registry.get_provider("myllm")
        assert isinstance(provider, OpenAICompatibleProvider)
        assert provider._url == "https://llm.example.com/v1"
        assert provider._key == "sk-custom"
        assert provider._models == ["my-model-a", "my-model-b"]
        assert "myllm" in registry.provider_names()
    finally:
        # Restore a clean registry for later tests — configure with no custom
        # backend removes the registered one.
        configure_custom_provider(Settings(environment="test", default_provider="echo"))

    assert registry.get_provider("myllm") is None


def test_no_custom_provider_without_base_url() -> None:
    # Sanity: the default registry (no custom_base_url) has no custom entry.
    import rekai.providers.registry as registry

    assert registry.get_provider("myllm") is None
    assert "custom" not in registry.provider_names()
