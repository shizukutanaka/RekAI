"""Tests for REKAI_MAX_CONCURRENT_REQUESTS (occupancy, not arrival rate).

The rate limiter counts requests *arriving*; this counts requests still
*running*. For an LLM gateway those diverge badly — 60 requests/min is
satisfiable by 60 concurrent 60-second streams — and nothing else in the stack
bounded the second quantity.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from rekai.config import Settings
from rekai.main import create_app
from rekai.providers import register_provider
from rekai.providers.base import EmbeddingResult, Provider, ProviderResult, StreamEvent
from rekai.schemas import Usage


class BlockingProvider(Provider):
    """Occupies a slot until the test releases it."""

    name = "blocking"
    requires_key = False

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def chat(self, request, api_key) -> ProviderResult:
        self.started.set()
        await self.release.wait()
        return ProviderResult(content="done", model=request.model, usage=Usage())

    async def stream_events(self, request, api_key):
        self.started.set()
        await self.release.wait()
        yield StreamEvent(delta="done")

    async def embed(self, inputs, model, api_key) -> EmbeddingResult:
        return EmbeddingResult(embeddings=[[0.0]], model=model, usage=Usage())

    async def list_models(self, api_key) -> list[str]:
        return ["blocking"]

    async def list_embedding_models(self, api_key) -> list[str]:
        return []


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _settings(**kw) -> Settings:
    kw.setdefault("max_concurrent_requests", 1)
    return Settings(environment="test", default_provider="blocking", rate_limit_enabled=False, **kw)


_BODY = {"model": "blocking", "messages": [{"role": "user", "content": "hi"}], "cache": False}


async def test_second_concurrent_request_is_rejected_with_retry_after() -> None:
    provider = BlockingProvider()
    register_provider(provider)
    app = create_app(_settings())

    async with _client(app) as client:
        first = asyncio.create_task(client.post("/v1/chat", json=_BODY))
        await asyncio.wait_for(provider.started.wait(), timeout=2)  # slot taken

        second = await client.post("/v1/chat", json=_BODY)
        assert second.status_code == 429
        assert second.json()["error"] == "concurrency_limit"
        assert second.headers["Retry-After"] == "1"

        provider.release.set()
        assert (await asyncio.wait_for(first, timeout=2)).status_code == 200


async def test_slot_is_released_so_the_next_request_succeeds() -> None:
    provider = BlockingProvider()
    register_provider(provider)
    app = create_app(_settings())

    async with _client(app) as client:
        provider.release.set()  # don't block: each request finishes immediately
        for _ in range(3):
            assert (await client.post("/v1/chat", json=_BODY)).status_code == 200


async def test_slot_is_held_until_the_last_body_chunk_is_sent() -> None:
    # The case the cap exists for. A BaseHTTPMiddleware dispatch returns from
    # call_next as soon as the response *starts*, so a slot released there would
    # be free before a streamed body had sent a single token — precisely the
    # long-running request this is meant to bound. Driven as raw ASGI because
    # httpx's ASGITransport buffers the whole body before returning, so it
    # cannot express "the response has started but is not finished".
    sending = asyncio.Event()
    finish = asyncio.Event()

    async def streaming_app(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        sending.set()
        await finish.wait()
        await send({"type": "http.response.body", "body": b"last", "more_body": False})

    from rekai.main import ConcurrencyLimitMiddleware

    middleware = ConcurrencyLimitMiddleware(streaming_app, max_concurrent=1)
    scope = {"type": "http", "method": "POST", "path": "/v1/chat", "headers": []}

    async def noop_receive() -> dict:  # pragma: no cover - never called here
        return {"type": "http.request", "body": b"", "more_body": False}

    def collector(into: list[dict]):
        async def send(message: dict) -> None:
            into.append(message)

        return send

    sent: list[dict] = []
    task = asyncio.create_task(middleware(scope, noop_receive, collector(sent)))
    await asyncio.wait_for(sending.wait(), timeout=2)

    # Response started, body incomplete: the slot must still be occupied.
    assert middleware._in_flight == 1
    rejected: list[dict] = []
    await middleware(scope, noop_receive, collector(rejected))
    assert rejected[0]["status"] == 429

    finish.set()
    await asyncio.wait_for(task, timeout=2)
    assert middleware._in_flight == 0


async def test_zero_is_unlimited() -> None:
    provider = BlockingProvider()
    register_provider(provider)
    app = create_app(_settings(max_concurrent_requests=0))

    async with _client(app) as client:
        first = asyncio.create_task(client.post("/v1/chat", json=_BODY))
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        second = asyncio.create_task(client.post("/v1/chat", json=_BODY))
        await asyncio.sleep(0)
        provider.release.set()
        for task in (first, second):
            assert (await asyncio.wait_for(task, timeout=2)).status_code == 200


async def test_non_v1_paths_are_not_capped() -> None:
    # /health and /metrics must stay answerable while /v1/* is saturated —
    # that is when an operator most needs them.
    provider = BlockingProvider()
    register_provider(provider)
    app = create_app(_settings())

    async with _client(app) as client:
        first = asyncio.create_task(client.post("/v1/chat", json=_BODY))
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/metrics")).status_code == 200
        provider.release.set()
        await asyncio.wait_for(first, timeout=2)


@pytest.mark.parametrize("path", ["/v1/chat", "/v1/embeddings"])
async def test_rejection_body_shape(path: str) -> None:
    provider = BlockingProvider()
    register_provider(provider)
    app = create_app(_settings())

    async with _client(app) as client:
        first = asyncio.create_task(client.post("/v1/chat", json=_BODY))
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        resp = await client.post(path, json={"model": "blocking", "input": "x", **_BODY})
        assert resp.status_code == 429
        assert set(resp.json()) == {"error", "detail"}
        provider.release.set()
        await asyncio.wait_for(first, timeout=2)
