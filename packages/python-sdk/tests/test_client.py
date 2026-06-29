"""Tests for the RekAI Python client using httpx MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from rekai_client import ChatResult, RekAIClient, RekAIError


def make_client(handler) -> RekAIClient:
    client = RekAIClient("http://test")
    client._client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))
    return client


def test_chat_returns_result() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["key"] = request.headers.get("X-Provider-Key")
        return httpx.Response(
            200,
            json={
                "id": "rekai-1",
                "provider": "echo",
                "model": "echo",
                "content": "Echo: hi",
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                "cost_usd": 0.0,
                "cached": False,
                "fallback_used": False,
            },
        )

    client = make_client(handler)
    result = client.chat("echo", "hi", provider_key="sk-x")
    assert isinstance(result, ChatResult)
    assert result.content == "Echo: hi"
    assert result.usage["total_tokens"] == 3
    # String message is normalized to a single user message.
    assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["url"].endswith("/v1/chat")
    assert captured["key"] == "sk-x"


def test_chat_passes_options() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "x",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "content": "ok",
                "usage": {},
                "cost_usd": None,
                "cached": False,
                "fallback_used": False,
            },
        )

    client = make_client(handler)
    client.chat(
        "gpt-4o-mini",
        [{"role": "user", "content": "hi"}],
        provider="openai",
        max_tokens=64,
        fallbacks=[{"provider": "echo"}],
    )
    body = captured["body"]
    assert body["provider"] == "openai"
    assert body["max_tokens"] == 64
    assert body["fallbacks"] == [{"provider": "echo"}]


def test_chat_raises_on_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "provider_error", "detail": "no key"})

    client = make_client(handler)
    with pytest.raises(RekAIError) as exc:
        client.chat("gpt-4o-mini", "hi")
    assert exc.value.status_code == 401
    assert "no key" in str(exc.value)


def test_stream_yields_deltas() -> None:
    sse = 'data: {"delta": "Hello"}\n\ndata: {"delta": " world"}\n\ndata: [DONE]\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    client = make_client(handler)
    chunks = list(client.stream("echo", "hi"))
    assert "".join(chunks) == "Hello world"


def test_stream_raises_on_error_event() -> None:
    sse = 'data: {"error": "provider_error", "detail": "boom"}\n\ndata: [DONE]\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    client = make_client(handler)
    with pytest.raises(RekAIError):
        list(client.stream("echo", "hi"))


def test_models_usage_health() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "echo", "provider": "echo"}]})
        if path == "/v1/usage":
            return httpx.Response(200, json={"requests_total": 5})
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    client = make_client(handler)
    assert client.models()[0]["id"] == "echo"
    assert client.usage()["requests_total"] == 5
    assert client.health()["status"] == "ok"


def test_context_manager() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    with make_client(handler) as client:
        assert client.health()["status"] == "ok"
