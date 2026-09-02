"""End-to-end proof that RekAI is a drop-in base_url for the real OpenAI SDK.

Skips when `openai` isn't installed (it's an optional dev dependency). Drives
the actual `AsyncOpenAI` client against the in-process app via httpx's
ASGITransport — no network, no running server.
"""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("openai")

import openai  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402

from rekai.config import Settings  # noqa: E402
from rekai.main import create_app  # noqa: E402


def _sdk_client(**overrides: object) -> AsyncOpenAI:
    settings: dict[str, object] = {
        "environment": "test",
        "default_provider": "echo",
        "rate_limit_enabled": False,
    }
    settings.update(overrides)
    app = create_app(Settings(**settings))  # type: ignore[arg-type]
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )
    return AsyncOpenAI(
        base_url="http://testserver/v1",
        api_key="sk-x",
        http_client=http_client,
        max_retries=0,
    )


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


async def test_openai_sdk_reads_a_gateway_rejection() -> None:
    """Errors are part of "no client changes" too.

    A 401 from RekAI's auth middleware never reached the route, so it kept
    RekAI's flat body and the SDK could make nothing of it: `exc.body` came
    through as the bare string `'unauthorized'` — `exc.body.get(...)` an
    AttributeError — with `exc.type` empty and the actual explanation reachable
    only by parsing the repr in `exc.message`."""
    client = _sdk_client(api_keys="sk-right")
    with pytest.raises(openai.AuthenticationError) as excinfo:
        try:
            await client.chat.completions.create(
                model="echo", messages=[{"role": "user", "content": "hello"}]
            )
        finally:
            await client.close()

    exc = excinfo.value
    assert isinstance(exc.body, dict)
    assert exc.type == "authentication_error"
    assert "API key" in exc.body["message"]


async def test_openai_sdk_reads_a_bad_request() -> None:
    """The commonest client error of all: a malformed body. FastAPI answers it
    with `{"detail": [ ...pydantic dicts... ]}`, which is neither shape.

    Asserted against `APIStatusError`, the base class, rather than
    `BadRequestError`: FastAPI validation is a **422**, and OpenAI's own API
    uses 400 for a bad parameter, so `except openai.BadRequestError` does not
    catch this one. That is a real divergence, but a status-code change is not
    this commit's subject — see docs/architecture.md."""
    client = _sdk_client()
    with pytest.raises(openai.APIStatusError) as excinfo:
        try:
            # `temperature` is a float field; a string cannot be coerced.
            await client.chat.completions.create(
                model="echo",
                messages=[{"role": "user", "content": "hello"}],
                temperature="warm",  # type: ignore[arg-type]
            )
        finally:
            await client.close()

    exc = excinfo.value
    assert isinstance(exc.body, dict)
    assert exc.type == "invalid_request_error"
    assert exc.param == "temperature"
