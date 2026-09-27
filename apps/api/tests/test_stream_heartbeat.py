"""SSE keepalives: a quiet upstream must not look like a dead connection.

Reasoning models can sit silent past a proxy's idle timeout; every SSE client
spec-compliant parser ignores comment lines (``: ka``), so emitting one is the
standard way to keep the wire warm without corrupting the event stream.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from rekai.config import Settings
from rekai.main import create_app
from rekai.providers import register_provider
from rekai.providers.base import Provider, ProviderResult, StreamEvent
from rekai.schemas import Usage
from rekai.service import ChatStreamEvent, with_heartbeat


async def _collect(stream: AsyncIterator[ChatStreamEvent | None]) -> list:
    return [ev async for ev in stream]


def _deltas_and_gaps(events: list) -> tuple[list[str], int]:
    deltas = [ev.delta for ev in events if ev is not None]
    return deltas, len(events) - len(deltas)


async def test_heartbeat_fires_when_upstream_goes_quiet() -> None:
    async def slow() -> AsyncIterator[ChatStreamEvent]:
        yield ChatStreamEvent(delta="a")
        await asyncio.sleep(0.06)
        yield ChatStreamEvent(delta="b")

    events = await _collect(with_heartbeat(slow(), interval=0.02))
    deltas, gaps = _deltas_and_gaps(events)
    assert deltas == ["a", "b"]
    assert gaps >= 1  # at least one heartbeat between them


async def test_disabled_interval_passes_events_through() -> None:
    async def fast() -> AsyncIterator[ChatStreamEvent]:
        yield ChatStreamEvent(delta="a")
        yield ChatStreamEvent(delta="b")

    events = await _collect(with_heartbeat(fast(), interval=0))
    deltas, gaps = _deltas_and_gaps(events)
    assert deltas == ["a", "b"]
    assert gaps == 0


async def test_quiet_stream_finishes_cleanly() -> None:
    """The watchdog must not hang waiting for a stream that already ended."""

    async def quiet() -> AsyncIterator[ChatStreamEvent]:
        if False:
            yield ChatStreamEvent(delta="never")

    assert await _collect(with_heartbeat(quiet(), interval=0.02)) == []


# --- end to end on /v1/chat/stream -------------------------------------------


class _SlowProvider(Provider):
    name = "slowp"
    requires_key = False

    async def chat(self, request, api_key):  # type: ignore[no-untyped-def]
        return ProviderResult(
            content="hi",
            model=request.model,
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    async def stream_events(self, request, api_key):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.06)
        yield StreamEvent(delta="hello")
        yield StreamEvent(
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            finish_reason="stop",
        )


def test_stream_emits_heartbeat_comments() -> None:
    register_provider(_SlowProvider())
    app = create_app(
        Settings(
            environment="test",
            default_provider="slowp",
            stream_heartbeat_seconds=0.02,
        )
    )
    resp = TestClient(app).post(
        "/v1/chat/stream",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert ": ka" in resp.text
    assert '"delta": "hello"' in resp.text


def test_stream_without_heartbeat_setting_has_no_comments() -> None:
    register_provider(_SlowProvider())
    app = create_app(
        Settings(
            environment="test",
            default_provider="slowp",
            stream_heartbeat_seconds=0,
        )
    )
    resp = TestClient(app).post(
        "/v1/chat/stream",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert ": ka" not in resp.text
    assert '"delta": "hello"' in resp.text
