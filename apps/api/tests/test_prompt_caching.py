"""Provider prompt-cache passthrough and discounted cost accounting (O-4).

Providers cache prompt prefixes at a steep discount (Anthropic on an explicit
``cache_control`` breakpoint, OpenAI automatically). RekAI passes the breakpoint
through, reports the cached/uncached split back to the caller, and prices the
cached slices at their own rates.
"""

from __future__ import annotations

import httpx

from rekai.cache import cache_key
from rekai.pricing import estimate_cost
from rekai.providers.anthropic import AnthropicProvider
from rekai.providers.openai import OpenAIProvider, _parse_openai_sse_event
from rekai.schemas import ChatMessage, ChatRequest, Usage


def _fake_post(monkeypatch, captured: dict, payload: dict) -> None:
    """Monkeypatch httpx.AsyncClient so one POST captures its body and replies."""

    class FakeResponse:
        status_code = 200
        headers: dict = {}

        def json(self) -> dict:
            return payload

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)


_ANTHROPIC_REPLY = {
    "model": "claude-sonnet-4-6",
    "content": [{"type": "text", "text": "ok"}],
    "usage": {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 90,
    },
}


# --- request-side passthrough ------------------------------------------------


async def test_top_level_cache_control_marks_last_message_block(monkeypatch) -> None:
    captured: dict = {}
    _fake_post(monkeypatch, captured, _ANTHROPIC_REPLY)

    await AnthropicProvider().chat(
        ChatRequest(
            model="claude-sonnet-4-6",
            messages=[ChatMessage(role="user", content="hi")],
            cache_control={"type": "ephemeral"},
        ),
        api_key="k",
    )
    # A string content is promoted to a text block so the marker has a home.
    last = captured["json"]["messages"][-1]["content"][-1]
    assert last["type"] == "text"
    assert last["cache_control"] == {"type": "ephemeral"}


async def test_per_message_cache_control_is_passed_through(monkeypatch) -> None:
    captured: dict = {}
    _fake_post(monkeypatch, captured, _ANTHROPIC_REPLY)

    await AnthropicProvider().chat(
        ChatRequest(
            model="claude-sonnet-4-6",
            messages=[
                ChatMessage(role="user", content="big prefix", cache_control={"type": "ephemeral"}),
                ChatMessage(role="user", content="question"),
            ],
        ),
        api_key="k",
    )
    msgs = captured["json"]["messages"]
    assert msgs[0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # The unmarked message keeps its plain string content.
    assert msgs[1]["content"] == "question"


async def test_no_cache_control_leaves_payload_unchanged(monkeypatch) -> None:
    captured: dict = {}
    _fake_post(monkeypatch, captured, _ANTHROPIC_REPLY)

    await AnthropicProvider().chat(
        ChatRequest(model="claude-sonnet-4-6", messages=[ChatMessage(role="user", content="hi")]),
        api_key="k",
    )
    assert captured["json"]["messages"][-1]["content"] == "hi"


# --- response-side accounting ------------------------------------------------


async def test_anthropic_usage_reports_cache_split(monkeypatch) -> None:
    _fake_post(monkeypatch, {}, _ANTHROPIC_REPLY)
    result = await AnthropicProvider().chat(
        ChatRequest(model="claude-sonnet-4-6", messages=[ChatMessage(role="user", content="hi")]),
        api_key="k",
    )
    assert result.usage.cache_read_tokens == 900
    assert result.usage.cache_write_tokens == 90
    # Anthropic reports cached tokens *outside* input_tokens; prompt_tokens is
    # folded back to the true prompt size (10 + 900 + 90).
    assert result.usage.prompt_tokens == 1000
    assert result.usage.total_tokens == 1005


async def test_openai_usage_reads_cached_tokens(monkeypatch) -> None:
    _fake_post(
        monkeypatch,
        {},
        {
            "model": "gpt-4o",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 5,
                "total_tokens": 1005,
                "prompt_tokens_details": {"cached_tokens": 900},
            },
        },
    )
    result = await OpenAIProvider().chat(
        ChatRequest(model="gpt-4o", messages=[ChatMessage(role="user", content="hi")]),
        api_key="k",
    )
    assert result.usage.cache_read_tokens == 900
    # OpenAI already counts cached tokens inside prompt_tokens — don't double it.
    assert result.usage.prompt_tokens == 1000
    assert result.usage.cache_write_tokens == 0


def test_openai_streaming_usage_reads_cached_tokens() -> None:
    line = (
        'data: {"usage": {"prompt_tokens": 1000, "completion_tokens": 5, '
        '"total_tokens": 1005, "prompt_tokens_details": {"cached_tokens": 900}}}'
    )
    event = _parse_openai_sse_event(line)
    assert event is not None and event.usage is not None
    assert event.usage.cache_read_tokens == 900


def test_usage_defaults_to_zero_cache_tokens() -> None:
    # Providers that report nothing cache-related stay at 0 (back-compatible).
    assert Usage(prompt_tokens=5).cache_read_tokens == 0
    assert Usage(prompt_tokens=5).cache_write_tokens == 0


# --- cost accounting ---------------------------------------------------------


def test_cached_reads_are_discounted() -> None:
    # gpt-4o: $2.50 per 1M input. 1000 prompt tokens, 900 of them cache reads.
    usage = Usage(prompt_tokens=1000, completion_tokens=0, total_tokens=1000, cache_read_tokens=900)
    cost = estimate_cost("openai", "gpt-4o", usage)
    # 100 fresh at 1.0x + 900 cached at 0.1x = 190 token-equivalents.
    assert cost == round(190 * 2.50 / 1_000_000, 6)
    # Strictly cheaper than paying full price for all 1000.
    assert cost < estimate_cost("openai", "gpt-4o", Usage(prompt_tokens=1000))


def test_cache_writes_cost_a_premium() -> None:
    usage = Usage(prompt_tokens=1000, cache_write_tokens=1000)
    cost = estimate_cost("openai", "gpt-4o", usage)
    # All 1000 written at 1.25x — more than the uncached price.
    assert cost == round(1000 * 2.50 * 1.25 / 1_000_000, 6)
    assert cost > estimate_cost("openai", "gpt-4o", Usage(prompt_tokens=1000))


def test_cached_tokens_are_not_double_counted() -> None:
    # Cached slices are a *breakdown* of prompt_tokens, not extra tokens: a fully
    # cached prompt bills only the discounted rate, never that plus full price.
    full = estimate_cost("openai", "gpt-4o", Usage(prompt_tokens=1000))
    all_cached = estimate_cost(
        "openai", "gpt-4o", Usage(prompt_tokens=1000, cache_read_tokens=1000)
    )
    assert all_cached == round(full * 0.1, 6)


def test_cost_unchanged_when_no_cache_tokens() -> None:
    usage = Usage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500)
    assert estimate_cost("openai", "gpt-4o", usage) == round(
        (1000 * 2.50 + 500 * 10.00) / 1_000_000, 6
    )


# --- cache key ---------------------------------------------------------------


def test_cache_control_keys_separately() -> None:
    base = ChatRequest(model="gpt-4o", messages=[ChatMessage(role="user", content="hi")])
    marked = ChatRequest(
        model="gpt-4o",
        messages=[ChatMessage(role="user", content="hi")],
        cache_control={"type": "ephemeral"},
    )
    per_message = ChatRequest(
        model="gpt-4o",
        messages=[ChatMessage(role="user", content="hi", cache_control={"type": "ephemeral"})],
    )
    assert cache_key(base, "openai") != cache_key(marked, "openai")
    assert cache_key(base, "openai") != cache_key(per_message, "openai")
