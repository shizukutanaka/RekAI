"""Tests for response_format passthrough across providers and the cache key."""

from __future__ import annotations

import httpx

from rekai.cache import cache_key
from rekai.providers.gemini import GeminiProvider
from rekai.providers.openai import OpenAIProvider
from rekai.schemas import ChatMessage, ChatRequest

JSON_OBJECT = {"type": "json_object"}
JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {"name": "person", "schema": {"type": "object"}},
}


def _fake_openai(monkeypatch, captured: dict) -> None:
    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {
                "model": "gpt-4o-mini",
                "choices": [{"message": {"content": "{}"}}],
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
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)


async def test_openai_forwards_response_format(monkeypatch) -> None:
    captured: dict = {}
    _fake_openai(monkeypatch, captured)
    req = ChatRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content="give me json")],
        response_format=JSON_OBJECT,
    )
    await OpenAIProvider().chat(req, api_key="sk-test")
    assert captured["payload"]["response_format"] == JSON_OBJECT


async def test_openai_omits_response_format_when_absent(monkeypatch) -> None:
    captured: dict = {}
    _fake_openai(monkeypatch, captured)
    req = ChatRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content="hi")],
    )
    await OpenAIProvider().chat(req, api_key="sk-test")
    assert "response_format" not in captured["payload"]


def test_gemini_maps_json_object_to_mime_type() -> None:
    req = ChatRequest(
        model="gemini-1.5-pro",
        messages=[ChatMessage(role="user", content="json please")],
        response_format=JSON_OBJECT,
    )
    payload = GeminiProvider()._build_payload(req)
    assert payload["generationConfig"]["responseMimeType"] == "application/json"
    assert "responseSchema" not in payload["generationConfig"]


def test_gemini_maps_json_schema_to_response_schema() -> None:
    req = ChatRequest(
        model="gemini-1.5-pro",
        messages=[ChatMessage(role="user", content="structured")],
        response_format=JSON_SCHEMA,
    )
    payload = GeminiProvider()._build_payload(req)
    assert payload["generationConfig"]["responseMimeType"] == "application/json"
    assert payload["generationConfig"]["responseSchema"] == {"type": "object"}


def test_response_format_changes_cache_key() -> None:
    base = ChatRequest(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content="same prompt")],
    )
    with_json = base.model_copy(update={"response_format": JSON_OBJECT})
    assert cache_key(base, "openai") != cache_key(with_json, "openai")
    # Same response_format -> same key (deterministic).
    with_json_2 = base.model_copy(update={"response_format": JSON_OBJECT})
    assert cache_key(with_json, "openai") == cache_key(with_json_2, "openai")


def test_compat_endpoint_passes_response_format(monkeypatch, client) -> None:
    # Register a capturing provider and drive it via the compat endpoint.
    from rekai.providers import register_provider
    from rekai.providers.base import Provider, ProviderResult
    from rekai.schemas import Usage

    captured: dict = {}

    class _CaptureProvider(Provider):
        name = "capture"
        requires_key = False

        async def chat(self, request, api_key):  # type: ignore[no-untyped-def]
            captured["response_format"] = request.response_format
            return ProviderResult(content="{}", model=request.model, usage=Usage())

    register_provider(_CaptureProvider())
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "provider": "capture",
            "messages": [{"role": "user", "content": "json"}],
            "response_format": JSON_OBJECT,
        },
    )
    assert resp.status_code == 200
    assert captured["response_format"] == JSON_OBJECT
