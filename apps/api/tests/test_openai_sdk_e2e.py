"""End-to-end proof that RekAI is a drop-in base_url for the real OpenAI SDK.

Skips when `openai` isn't installed (it's an optional dev dependency). Drives
the actual `AsyncOpenAI` client against the in-process app via httpx's
ASGITransport — no network, no running server.
"""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("openai")

from openai import AsyncOpenAI  # noqa: E402

from rekai.config import Settings  # noqa: E402
from rekai.main import create_app  # noqa: E402


def _sdk_client() -> AsyncOpenAI:
    app = create_app(
        Settings(environment="test", default_provider="echo", rate_limit_enabled=False)
    )
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )
    return AsyncOpenAI(base_url="http://testserver/v1", api_key="sk-x", http_client=http_client)


async def test_openai_sdk_non_streaming() -> None:
    client = _sdk_client()
    try:
        result = await client.chat.completions.create(
            model="echo", messages=[{"role": "user", "content": "hello"}]
        )
    finally:
        await client.close()
    assert result.object == "chat.completion"
    assert result.choices[0].message.content == "Echo: hello"
    assert result.choices[0].finish_reason == "stop"
    assert result.usage is not None and result.usage.total_tokens > 0


async def test_openai_sdk_streaming() -> None:
    client = _sdk_client()
    text = ""
    try:
        stream = await client.chat.completions.create(
            model="echo",
            messages=[{"role": "user", "content": "hello world"}],
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                text += chunk.choices[0].delta.content
    finally:
        await client.close()
    assert text == "Echo: hello world"
