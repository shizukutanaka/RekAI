"""Anthropic extended thinking — request config, history echo, response blocks.

`thinking` used to be swallowed by ``extra="allow"``: an SDK caller asking for
extended thinking got a plain answer, and thinking blocks echoed back in
assistant history were a 400. Both halves now ride verbatim to Anthropic.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from rekai.cache import NullCache
from rekai.config import Settings
from rekai.providers import register_provider
from rekai.providers.anthropic import AnthropicProvider
from rekai.providers.base import Provider, ProviderResult, StreamEvent
from rekai.schemas import ChatMessage, ChatRequest, Usage
from rekai.semantic_cache import semantic_cache
from rekai.service import handle_chat, handle_chat_stream


def _payload(**over) -> dict:
    body = {
        "model": "echo",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hello"}],
    }
    body.update(over)
    return body


def _anthropic_request(resp_messages) -> ChatRequest:
    from rekai import anthropic_compat
    from rekai.schemas import AnthropicMessagesRequest

    req = AnthropicMessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 2000,
            "messages": resp_messages,
            **{"thinking": {"type": "enabled", "budget_tokens": 1024}},
        }
    )
    return anthropic_compat.to_chat_request(req)


class _FakeResponse:
    status_code = 200

    def json(self) -> dict:
        return {
            "model": "claude-sonnet-4-6",
            "content": [
                {"type": "thinking", "thinking": "let me see…", "signature": "sig1"},
                {"type": "text", "text": "the answer"},
            ],
            "usage": {"input_tokens": 5, "output_tokens": 9},
        }


class _FakeClient:
    captured: dict = {}

    def __init__(self, *a, **k) -> None:
        pass

    async def aclose(self) -> None:
        return None

    async def post(self, url, json=None, headers=None):
        _FakeClient.captured = json
        return _FakeResponse()


# --- request side ------------------------------------------------------------


def test_thinking_reaches_anthropic_payload(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    resp = client.post(
        "/v1/messages",
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 2000,
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"X-Provider-Key": "k"},
    )
    assert resp.status_code == 200
    assert _FakeClient.captured["thinking"] == {"type": "enabled", "budget_tokens": 1024}


def test_thinking_defaults_temperature_to_one(client: TestClient, monkeypatch) -> None:
    """Anthropic requires temperature=1 under thinking; an unset caller
    temperature should get Anthropic's own default, not our 0.7."""
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    resp = client.post(
        "/v1/messages",
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 2000,
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"X-Provider-Key": "k"},
    )
    assert resp.status_code == 200
    assert _FakeClient.captured["temperature"] == 1.0


def test_explicit_temperature_is_kept_under_thinking(client: TestClient, monkeypatch) -> None:
    """A caller-stated temperature rides as sent — Anthropic's own 400 then
    explains the constraint instead of us rewriting the request."""
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    resp = client.post(
        "/v1/messages",
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 2000,
            "temperature": 0.7,
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"X-Provider-Key": "k"},
    )
    assert resp.status_code == 200
    assert _FakeClient.captured["temperature"] == 0.7


# --- history echo ------------------------------------------------------------


def test_thinking_blocks_echo_verbatim_to_anthropic(client: TestClient, monkeypatch) -> None:
    """A multi-turn SDK loop sends prior thinking blocks back; they used to be
    a 400, now they ride on the assistant message — first, before text."""
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    thinking_block = {"type": "thinking", "thinking": "prior reasoning", "signature": "sig9"}
    resp = client.post(
        "/v1/messages",
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 2000,
            "messages": [
                {"role": "user", "content": "q1"},
                {
                    "role": "assistant",
                    "content": [
                        thinking_block,
                        {"type": "text", "text": "a1"},
                    ],
                },
                {"role": "user", "content": "q2"},
            ],
        },
        headers={"X-Provider-Key": "k"},
    )
    assert resp.status_code == 200
    assistant = _FakeClient.captured["messages"][1]
    assert assistant["content"][0] == thinking_block
    assert assistant["content"][1] == {"type": "text", "text": "a1"}


def test_redacted_thinking_echoed_verbatim(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    redacted = {"type": "redacted_thinking", "data": "opaque-blob"}
    resp = client.post(
        "/v1/messages",
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 2000,
            "messages": [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": [redacted, {"type": "text", "text": "a1"}]},
                {"role": "user", "content": "q2"},
            ],
        },
        headers={"X-Provider-Key": "k"},
    )
    assert resp.status_code == 200
    assert _FakeClient.captured["messages"][1]["content"][0] == redacted


def test_thinking_block_on_user_turn_is_a_400(client: TestClient) -> None:
    resp = client.post(
        "/v1/messages",
        json=_payload(
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "thinking", "thinking": "sneaky", "signature": "x"}],
                }
            ]
        ),
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"


# --- response side -----------------------------------------------------------


def test_thinking_blocks_lead_the_response_content(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    resp = client.post(
        "/v1/messages",
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 2000,
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"X-Provider-Key": "k"},
    )
    assert resp.status_code == 200
    content = resp.json()["content"]
    assert content[0] == {"type": "thinking", "thinking": "let me see…", "signature": "sig1"}
    assert content[1] == {"type": "text", "text": "the answer"}


def test_thinking_response_blocks_not_on_openai_surface(client: TestClient, monkeypatch) -> None:
    """OpenAI's schema has no thinking channel — the field is dropped there
    rather than mangled into something an OpenAI SDK would mis-parse."""
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-sonnet-4-6",
            "provider": "anthropic",
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"X-Provider-Key": "k"},
    )
    assert resp.status_code == 200
    message = resp.json()["choices"][0]["message"]
    assert "thinking_blocks" not in message
    assert message["content"] == "the answer"


# --- streaming ---------------------------------------------------------------


async def _stream_events(provider, request, key="k"):
    out = []
    async for ev in provider.stream_events(request, key):
        out.append(ev)
    return out


@pytest.mark.asyncio
async def test_stream_emits_thinking_deltas(monkeypatch) -> None:
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "ponder"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "answer"},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 4},
        },
        {"type": "message_stop"},
    ]
    sse_body = "".join(f"data: {json.dumps(e)}\n\n" for e in events)

    class FakeStreamClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def aclose(self) -> None:
            return None

        def stream(self, method, url, json=None, headers=None):
            class _CM:
                async def __aenter__(self_inner):
                    class _Resp:
                        status_code = 200

                        async def aiter_lines(self_resp):
                            for line in sse_body.splitlines():
                                yield line

                    return _Resp()

                async def __aexit__(self_inner, *exc):
                    return False

            return _CM()

    monkeypatch.setattr(httpx, "AsyncClient", FakeStreamClient)
    provider = AnthropicProvider()
    request = ChatRequest(
        model="claude-sonnet-4-6",
        messages=[ChatMessage(role="user", content="hi")],
        thinking={"type": "enabled", "budget_tokens": 1024},
    )
    events = await _stream_events(provider, request)
    kinds = [
        ("thinking_delta" if e.thinking_delta else None)
        or ("signature" if e.thinking_signature else None)
        or ("delta" if e.delta else None)
        or ("summary" if e.usage or e.finish_reason else None)
        for e in events
    ]
    assert kinds[:3] == ["thinking_delta", "signature", "delta"]


def test_stream_thinking_reaches_anthropic_sse(client: TestClient, monkeypatch) -> None:
    """End-to-end: a thinking_delta upstream becomes Anthropic's typed SSE
    sequence (content_block_start thinking -> thinking_delta -> signature_delta
    -> content_block_stop), followed by the text block at the next index."""
    from rekai.providers.base import StreamEvent

    async def fake_stream_events(self, request, api_key):
        yield StreamEvent(thinking_delta="ponder")
        yield StreamEvent(thinking_signature="sig")
        yield StreamEvent(delta="answer")
        yield StreamEvent(finish_reason="stop")

    monkeypatch.setattr(AnthropicProvider, "stream_events", fake_stream_events)
    resp = client.post(
        "/v1/messages",
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 2000,
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
        headers={"X-Provider-Key": "k"},
    )
    assert resp.status_code == 200
    events = []
    current = None
    for line in resp.text.splitlines():
        if line.startswith("event:"):
            current = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and current:
            events.append((current, json.loads(line.split(":", 1)[1])))
            current = None
    names = [e for e, _ in events]
    thinking_start = names.index("content_block_start")
    assert events[thinking_start][1]["content_block"]["type"] == "thinking"
    delta_ev = events[thinking_start + 1][1]["delta"]
    assert delta_ev == {"type": "thinking_delta", "thinking": "ponder"}
    sig_ev = events[thinking_start + 2][1]["delta"]
    assert sig_ev == {"type": "signature_delta", "signature": "sig"}
    assert events[thinking_start + 3][0] == "content_block_stop"
    # Text block follows at the next index.
    text_start = names.index("content_block_start", thinking_start + 1)
    assert events[text_start][1] == {
        "type": "content_block_start",
        "index": 1,
        "content_block": {"type": "text", "text": ""},
    }


# --- cache separation --------------------------------------------------------


def test_thinking_is_part_of_the_cache_key() -> None:
    from rekai.cache import cache_key

    base = ChatRequest(model="echo", messages=[ChatMessage(role="user", content="hi")])
    thinking = base.model_copy(update={"thinking": {"type": "enabled", "budget_tokens": 512}})
    assert cache_key(base, "echo") != cache_key(thinking, "echo")


# --- post-merge review follow-ups -------------------------------------------
# (a) held-back thinking must reach the client before its signature — the SSE
# builder drops signatures with no open block, and Anthropic needs the pair to
# replay the history; (b) thinking text is model output: secrets in it get the
# same scrubbing as the answer; (c) the semantic index keys on plain text, so
# thinking history would collide — skip it; (d) Anthropic strips prior
# thinking from billed context, so count_tokens must not estimate it.


class _PonderingProvider(Provider):
    """Streams a thinking block short enough to sit inside the redactor's
    holdback — the case that used to strand its signature."""

    name = "svc-pondering"
    requires_key = False

    async def chat(self, request, api_key):  # pragma: no cover - unused here
        raise NotImplementedError

    async def stream_events(self, request, api_key):
        yield StreamEvent(thinking_delta="ponder")
        yield StreamEvent(thinking_signature="sig-1")
        yield StreamEvent(delta="answer")
        yield StreamEvent(usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2))


async def test_signature_waits_for_held_back_thinking() -> None:
    provider = _PonderingProvider()
    register_provider(provider)
    events = [
        e
        async for e in handle_chat_stream(
            ChatRequest(
                model="x",
                provider="svc-pondering",
                messages=[ChatMessage(role="user", content="hi")],
            ),
            None,
            Settings(environment="test", default_provider="echo", output_redaction_enabled=True),
            NullCache(),
            "svc-pondering",
            provider,
            "client-a",
        )
    ]
    kinds = [
        ("thinking_delta" if e.thinking_delta else None)
        or ("signature" if e.thinking_signature else None)
        or ("delta" if e.delta else None)
        or ("summary" if e.summary else None)
        for e in events
    ]
    assert kinds[:3] == ["thinking_delta", "signature", "delta"]
    assert events[0].thinking_delta == "ponder"
    assert events[1].thinking_signature == "sig-1"


async def test_thinking_text_is_secret_scrubbed(monkeypatch) -> None:
    from rekai.providers.echo import EchoProvider

    secret = "sk-" + "e" * 30

    async def fake_chat(self, request, api_key):
        return ProviderResult(
            model="echo",
            content="safe answer",
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            thinking_blocks=[
                {"type": "thinking", "thinking": f"ponder {secret}", "signature": "s"},
                {"type": "redacted_thinking", "data": "opaque-blob"},
            ],
        )

    monkeypatch.setattr(EchoProvider, "chat", fake_chat)
    settings = Settings(environment="test", default_provider="echo", output_redaction_enabled=True)
    result = await handle_chat(
        ChatRequest(model="echo", messages=[ChatMessage(role="user", content="hi")]),
        None,
        settings,
        NullCache(),
    )
    assert secret not in result.thinking_blocks[0]["thinking"]
    assert "[REDACTED:openai_api_key]" in result.thinking_blocks[0]["thinking"]
    assert "openai_api_key" in result.redacted
    assert result.thinking_blocks[1]["data"] == "opaque-blob"


async def test_thinking_history_skips_the_semantic_cache() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        cache_enabled=False,
        semantic_cache_enabled=True,
        semantic_cache_model="echo",
    )
    semantic_cache.clear()
    request = ChatRequest(
        model="echo",
        messages=[
            ChatMessage(
                role="assistant",
                content="Done",
                thinking_blocks=[{"type": "thinking", "thinking": "Plan A", "signature": "s"}],
            ),
            ChatMessage(role="user", content="Continue"),
        ],
    )
    first = await handle_chat(request, None, settings, NullCache())
    second = await handle_chat(request, None, settings, NullCache())
    assert first.cached is False
    assert second.cached is False
    semantic_cache.clear()


def test_count_tokens_ignores_prior_thinking_blocks() -> None:
    from rekai.anthropic_compat import count_tokens
    from rekai.schemas import AnthropicCountTokensRequest

    base = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 64,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "Done"}]},
            {"role": "user", "content": "continue"},
        ],
    }
    without = count_tokens(AnthropicCountTokensRequest.model_validate(base))
    with_thinking = count_tokens(
        AnthropicCountTokensRequest.model_validate(
            {
                **base,
                "messages": [
                    {"role": "user", "content": "hi"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "thinking",
                                "thinking": "long reasoning " * 500,
                                "signature": "s",
                            },
                            {"type": "text", "text": "Done"},
                        ],
                    },
                    {"role": "user", "content": "continue"},
                ],
            }
        )
    )
    assert with_thinking == without
