"""Tests for the RekAI Python client using httpx MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from rekai_client import ChatResult, EmbeddingsResult, RekAIClient, RekAIError


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


def test_chat_forwards_tools_and_returns_tool_calls() -> None:
    captured = {}
    tool_call = {"id": "c1", "type": "function", "function": {"name": "get_weather"}}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "x",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "content": "",
                "tool_calls": [tool_call],
                "usage": {},
                "cost_usd": None,
                "cached": False,
                "fallback_used": False,
            },
        )

    client = make_client(handler)
    tools = [{"type": "function", "function": {"name": "get_weather"}}]
    result = client.chat("gpt-4o-mini", "weather?", tools=tools, tool_choice="auto")
    assert captured["body"]["tools"] == tools
    assert captured["body"]["tool_choice"] == "auto"
    assert result.tool_calls == [tool_call]


def test_chat_forwards_response_format() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "x",
                "provider": "openai",
                "model": "gpt-4o-mini",
                "content": "{}",
                "usage": {},
                "cost_usd": None,
                "cached": False,
                "fallback_used": False,
            },
        )

    client = make_client(handler)
    client.chat("gpt-4o-mini", "give me json", response_format={"type": "json_object"})
    assert captured["body"]["response_format"] == {"type": "json_object"}


def test_chat_omits_response_format_when_absent() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "x",
                "provider": "echo",
                "model": "echo",
                "content": "ok",
                "usage": {},
                "cost_usd": None,
                "cached": False,
                "fallback_used": False,
            },
        )

    client = make_client(handler)
    client.chat("echo", "hi")
    assert "response_format" not in captured["body"]


def test_chat_forwards_gateway_key_as_bearer_header() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "id": "x",
                "provider": "echo",
                "model": "echo",
                "content": "ok",
                "usage": {},
                "cost_usd": None,
                "cached": False,
                "fallback_used": False,
            },
        )

    client = make_client(handler)
    client._gateway_key = "sk-rekai-default"
    client.chat("echo", "hi")
    assert captured["auth"] == "Bearer sk-rekai-default"

    client.chat("echo", "hi", gateway_key="sk-rekai-override")
    assert captured["auth"] == "Bearer sk-rekai-override"


def test_models_and_usage_forward_gateway_key() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"requests_total": 0})

    client = make_client(handler)
    client.models(gateway_key="sk-rekai-1")
    assert captured["auth"] == "Bearer sk-rekai-1"

    client.usage(gateway_key="sk-rekai-2")
    assert captured["auth"] == "Bearer sk-rekai-2"


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


def test_stream_invokes_on_usage() -> None:
    sse = (
        'data: {"delta": "Hi"}\n\n'
        'data: {"provider":"echo","model":"echo",'
        '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2},'
        '"cost_usd":0.0,"estimated":false}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    client = make_client(handler)
    seen: dict = {}
    chunks = list(client.stream("echo", "hi", on_usage=lambda s: seen.update(s)))
    assert "".join(chunks) == "Hi"
    assert seen["usage"]["total_tokens"] == 2
    assert seen["estimated"] is False


def test_stream_raises_on_error_event() -> None:
    sse = 'data: {"error": "provider_error", "detail": "boom"}\n\ndata: [DONE]\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    client = make_client(handler)
    with pytest.raises(RekAIError):
        list(client.stream("echo", "hi"))


def test_embeddings_returns_result() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["key"] = request.headers.get("X-Provider-Key")
        return httpx.Response(
            200,
            json={
                "provider": "echo",
                "model": "echo",
                "embeddings": [[0.1, 0.2], [0.3, 0.4]],
                "usage": {"prompt_tokens": 2, "completion_tokens": 0, "total_tokens": 2},
                "cost_usd": 0.0,
                "cached": False,
            },
        )

    client = make_client(handler)
    result = client.embeddings("echo", ["a", "b"], provider="echo", provider_key="sk-e")
    assert isinstance(result, EmbeddingsResult)
    assert result.embeddings == [[0.1, 0.2], [0.3, 0.4]]
    assert result.usage["total_tokens"] == 2
    assert result.cost_usd == 0.0
    assert captured["body"] == {
        "model": "echo",
        "input": ["a", "b"],
        "cache": True,
        "provider": "echo",
    }
    assert captured["url"].endswith("/v1/embeddings")
    assert captured["key"] == "sk-e"


def test_embeddings_accepts_string_input() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "provider": "echo",
                "model": "echo",
                "embeddings": [[0.5]],
                "usage": {},
                "cached": True,
            },
        )

    client = make_client(handler)
    result = client.embeddings("echo", "hello", cache=False)
    assert captured["body"]["input"] == "hello"
    assert captured["body"]["cache"] is False
    assert result.cached is True


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
