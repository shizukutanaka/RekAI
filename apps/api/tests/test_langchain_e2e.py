"""End-to-end proof that RekAI is a drop-in base_url for LangChain.

The README's first feature bullet promises that "any OpenAI SDK, LangChain, or
OpenAI-format client" works unmodified. `test_openai_sdk_e2e.py` proves that for
the OpenAI SDK; this proves it for the other client named, which is not the same
test — `langchain_openai.ChatOpenAI` sends fields the plain SDK does not (notably
`stream_options.include_usage`) and reads the response back through its own
`AIMessage` mapping, so a compatibility gap would surface here and nowhere else.

Skips when `langchain-openai` isn't installed (an optional dev dependency).
Drives the real client against the in-process app via httpx's ASGITransport —
no network, no running server.
"""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("langchain_openai")

from langchain_openai import ChatOpenAI  # noqa: E402

from rekai.config import Settings  # noqa: E402
from rekai.main import create_app  # noqa: E402


def _llm(**kwargs) -> ChatOpenAI:
    app = create_app(
        Settings(environment="test", default_provider="echo", rate_limit_enabled=False)
    )
    return ChatOpenAI(
        model="echo",
        base_url="http://testserver/v1",
        api_key="sk-x",
        http_async_client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ),
        **kwargs,
    )


async def test_langchain_invoke() -> None:
    message = await _llm().ainvoke("hello from langchain")

    assert message.content == "Echo: hello from langchain"
    # LangChain reads these off the OpenAI-shaped response; a wrong shape makes
    # them silently None rather than failing loudly.
    assert message.response_metadata["finish_reason"] == "stop"
    assert message.response_metadata["model_name"] == "echo"
    assert message.usage_metadata is not None
    assert message.usage_metadata["total_tokens"] > 0


async def test_langchain_streaming() -> None:
    chunks = [chunk async for chunk in _llm().astream("hello world")]

    assert "".join(str(chunk.content) for chunk in chunks) == "Echo: hello world"
    assert any(chunk.response_metadata.get("finish_reason") == "stop" for chunk in chunks)


async def test_langchain_streaming_reports_usage() -> None:
    # stream_usage=True makes LangChain send `stream_options: {include_usage:
    # true}` and expect a trailing usage-only chunk (`choices: []`). Emitting
    # only content chunks would leave every LangChain caller's token accounting
    # at zero while looking like it worked.
    chunks = [chunk async for chunk in _llm(stream_usage=True).astream("hi")]

    usage = [chunk.usage_metadata for chunk in chunks if chunk.usage_metadata]
    assert len(usage) == 1
    assert usage[0]["input_tokens"] > 0
    assert usage[0]["output_tokens"] > 0
    assert usage[0]["total_tokens"] == usage[0]["input_tokens"] + usage[0]["output_tokens"]
    # The usage chunk must not also carry text, or the content would be doubled.
    assert "".join(str(chunk.content) for chunk in chunks) == "Echo: hi"
