"""Tests for the RekAI Python client using httpx MockTransport."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from rekai_client import (
    AsyncRekAIClient,
    ChatResult,
    EmbeddingsResult,
    RekAIClient,
    RekAIError,
)


def make_client(handler) -> RekAIClient:
    client = RekAIClient("http://test")
    client._client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))
    return client


def make_async_client(handler) -> AsyncRekAIClient:
    client = AsyncRekAIClient("http://test")
    client._client = httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    )
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


def test_stream_invokes_on_tool_calls() -> None:
    sse = (
        'data: {"delta": "Hi"}\n\n'
        'data: {"provider":"echo","model":"echo","usage":{"total_tokens":2},'
        '"cost_usd":0.0,"estimated":false,'
        '"tool_calls":[{"id":"c1","type":"function",'
        '"function":{"name":"get_weather","arguments":"{}"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    client = make_client(handler)
    seen: dict = {}
    tool_calls: list = []
    list(
        client.stream(
            "echo",
            "hi",
            on_usage=lambda s: seen.update(s),
            on_tool_calls=lambda t: tool_calls.extend(t),
        )
    )
    assert tool_calls and tool_calls[0]["function"]["name"] == "get_weather"
    # Still present on the usage summary too (unchanged wire shape).
    assert seen["tool_calls"][0]["id"] == "c1"


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


def _chat_ok(request: httpx.Request) -> httpx.Response:
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


# --- Idempotency-Key + client-side retry (S-11) ------------------------------


def test_chat_sends_explicit_idempotency_key() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("Idempotency-Key")
        return _chat_ok(request)

    client = make_client(handler)
    client.chat("echo", "hi", idempotency_key="my-key-123")
    assert seen["key"] == "my-key-123"


def test_chat_auto_generates_idempotency_key_when_retries_enabled() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("Idempotency-Key")
        return _chat_ok(request)

    client = make_client(handler)  # default max_retries=2
    client.chat("echo", "hi")
    assert seen["key"] and seen["key"].startswith("rekai-sdk-")


def test_chat_omits_idempotency_key_when_retries_disabled() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("Idempotency-Key")
        return _chat_ok(request)

    client = make_client(handler)
    client._max_retries = 0
    client.chat("echo", "hi")
    assert seen["key"] is None


def test_retry_on_429_reuses_the_same_idempotency_key() -> None:
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers.get("Idempotency-Key"))
        if len(attempts) == 1:
            return httpx.Response(429, json={"detail": "slow down"})
        return _chat_ok(request)

    client = make_client(handler)
    client._retry_backoff = 0  # no real sleeping in tests
    result = client.chat("echo", "hi", idempotency_key="k1")
    assert result.content == "ok"
    assert attempts == ["k1", "k1"]  # retried once, same key both times


def test_retry_on_transport_error_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return _chat_ok(request)

    client = make_client(handler)
    client._retry_backoff = 0
    assert client.chat("echo", "hi").content == "ok"
    assert calls["n"] == 2


def test_retry_gives_up_and_raises_after_max_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "down"})

    client = make_client(handler)
    client._max_retries = 1
    client._retry_backoff = 0
    with pytest.raises(RekAIError) as exc:
        client.chat("echo", "hi")
    # After exhausting retries the last 503 surfaces as-is (the SDK echoes the
    # response status; it doesn't normalize 5xx the way the server does).
    assert exc.value.status_code == 503


def test_async_retry_on_429_then_succeeds() -> None:
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers.get("Idempotency-Key"))
        if len(attempts) == 1:
            return httpx.Response(429, json={"detail": "slow down"})
        return _chat_ok(request)

    async def run() -> ChatResult:
        client = make_async_client(handler)
        client._retry_backoff = 0
        async with client:
            return await client.chat("echo", "hi", idempotency_key="ka")

    result = asyncio.run(run())
    assert result.content == "ok"
    assert attempts == ["ka", "ka"]


# --- AsyncRekAIClient (driven via asyncio.run to avoid a pytest-asyncio dep) --


def test_async_chat_returns_result() -> None:
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

    async def run() -> ChatResult:
        async with make_async_client(handler) as client:
            return await client.chat("echo", "hi", provider_key="sk-x")

    result = asyncio.run(run())
    assert isinstance(result, ChatResult)
    assert result.content == "Echo: hi"
    assert result.usage["total_tokens"] == 3
    assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["url"].endswith("/v1/chat")
    assert captured["key"] == "sk-x"


def test_async_chat_raises_on_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "provider_error", "detail": "no key"})

    async def run() -> None:
        async with make_async_client(handler) as client:
            await client.chat("gpt-4o-mini", "hi")

    with pytest.raises(RekAIError) as exc:
        asyncio.run(run())
    assert exc.value.status_code == 401
    assert "no key" in str(exc.value)


def test_async_stream_yields_deltas_and_usage() -> None:
    sse = (
        'data: {"delta": "Hello"}\n\n'
        'data: {"delta": " world"}\n\n'
        'data: {"provider":"echo","model":"echo",'
        '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2},'
        '"cost_usd":0.0,"estimated":false}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    seen: dict = {}

    async def run() -> list[str]:
        chunks: list[str] = []
        async with make_async_client(handler) as client:
            async for chunk in client.stream("echo", "hi", on_usage=lambda s: seen.update(s)):
                chunks.append(chunk)
        return chunks

    chunks = asyncio.run(run())
    assert "".join(chunks) == "Hello world"
    assert seen["usage"]["total_tokens"] == 2


def test_async_stream_awaits_coroutine_on_usage() -> None:
    sse = (
        'data: {"delta": "Hi"}\n\n'
        'data: {"provider":"echo","model":"echo","usage":{"total_tokens":2},'
        '"cost_usd":0.0,"estimated":false}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    seen: dict = {}

    async def run() -> None:
        async def on_usage(summary: dict) -> None:
            seen.update(summary)

        async with make_async_client(handler) as client:
            async for _ in client.stream("echo", "hi", on_usage=on_usage):
                pass

    asyncio.run(run())
    assert seen["usage"]["total_tokens"] == 2


def test_async_stream_invokes_on_tool_calls() -> None:
    sse = (
        'data: {"delta": "Hi"}\n\n'
        'data: {"provider":"echo","model":"echo","usage":{"total_tokens":2},'
        '"cost_usd":0.0,"estimated":false,'
        '"tool_calls":[{"id":"c1","type":"function",'
        '"function":{"name":"get_weather","arguments":"{}"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    tool_calls: list = []

    async def run() -> None:
        async def on_tool_calls(tcs: list) -> None:
            tool_calls.extend(tcs)

        async with make_async_client(handler) as client:
            async for _ in client.stream("echo", "hi", on_tool_calls=on_tool_calls):
                pass

    asyncio.run(run())
    assert tool_calls and tool_calls[0]["function"]["name"] == "get_weather"


def test_async_stream_raises_on_error_event() -> None:
    sse = 'data: {"error": "provider_error", "detail": "boom"}\n\ndata: [DONE]\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse)

    async def run() -> None:
        async with make_async_client(handler) as client:
            async for _ in client.stream("echo", "hi"):
                pass

    with pytest.raises(RekAIError):
        asyncio.run(run())


def test_async_embeddings_and_gateway_key() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "provider": "echo",
                "model": "echo",
                "embeddings": [[0.1, 0.2]],
                "usage": {"total_tokens": 2},
                "cost_usd": 0.0,
                "cached": False,
            },
        )

    async def run() -> EmbeddingsResult:
        async with make_async_client(handler) as client:
            return await client.embeddings("echo", "hi", gateway_key="sk-rekai-1")

    result = asyncio.run(run())
    assert isinstance(result, EmbeddingsResult)
    assert result.embeddings == [[0.1, 0.2]]
    assert captured["auth"] == "Bearer sk-rekai-1"


def test_async_models_usage_health() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "echo", "provider": "echo"}]})
        if path == "/v1/usage":
            return httpx.Response(200, json={"requests_total": 5})
        return httpx.Response(200, json={"status": "ok"})

    async def run() -> tuple:
        async with make_async_client(handler) as client:
            return (await client.models(), await client.usage(), await client.health())

    models, usage, health = asyncio.run(run())
    assert models[0]["id"] == "echo"
    assert usage["requests_total"] == 5
    assert health["status"] == "ok"


# --- Retry-After is bounded ---------------------------------------------------
# The gateway deliberately refuses to wait longer than REKAI_RETRY_MAX_DELAY_SECONDS
# and passes the header to the caller instead, "so its SDK can back off precisely
# (rather than blocking the gateway)". The SDK then slept for whatever the header
# said — a `Retry-After: 3600` parked `chat()` for an hour, blocking the thread —
# which undid the server's design rather than completing it.


def _resp(retry_after):
    import httpx

    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return httpx.Response(429, headers=headers)


def test_retry_after_within_the_cap_is_honored_exactly():
    from rekai_client.client import _retry_delay

    assert _retry_delay(_resp("30"), 0, 0.5, 60.0) == 30.0


def test_retry_after_beyond_the_cap_stops_the_retry():
    # None means "return the response to the caller", not "wait 0". Retrying
    # sooner than the server asked would only earn another 429.
    from rekai_client.client import _retry_delay

    assert _retry_delay(_resp("3600"), 0, 0.5, 60.0) is None


def test_an_empty_retry_after_falls_back_to_backoff():
    from rekai_client.client import _retry_delay

    assert _retry_delay(_resp(""), 0, 0.5, 60.0) == 0.5


def test_an_http_date_retry_after_falls_back_to_backoff():
    from rekai_client.client import _retry_delay

    assert _retry_delay(_resp("Wed, 21 Oct 2015 07:28:00 GMT"), 1, 0.5, 60.0) == 1.0


def test_a_long_retry_after_returns_the_response_instead_of_sleeping():
    # End to end through _send: the caller gets the 429 back, with the header
    # still on it, rather than the process sitting inside time.sleep(3600).
    import time

    import httpx

    from rekai_client import RekAIClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3600"}, json={"detail": "slow down"})

    client = RekAIClient("http://testserver", max_retries=3)
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://testserver"
    )

    started = time.monotonic()
    resp = client._send("GET", "/v1/usage")

    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "3600"
    assert time.monotonic() - started < 5.0
