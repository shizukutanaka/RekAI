"""`stop` must reach every provider, and must key the cache.

OpenAI's `stop` is a *control* parameter — it decides where generation ends —
not a tuning knob like `seed` or `logit_bias`, which ChatCompletionsRequest
deliberately tolerates and ignores. RekAI accepted it (via `extra="allow"`),
returned 200, and never sent it to anyone, so the model ran past the point the
caller asked it to stop and billed them for the difference. All four backends
support it; only the name differs.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from rekai.cache import cache_key, semantic_bucket
from rekai.config import Settings
from rekai.main import create_app
from rekai.providers.anthropic import AnthropicProvider
from rekai.providers.gemini import GeminiProvider
from rekai.providers.ollama import OllamaProvider
from rekai.providers.openai import OpenAIProvider
from rekai.schemas import ChatMessage, ChatRequest

STOP = ["\n", "END"]


def _req(**kw: object) -> ChatRequest:
    kw.setdefault("model", "m")
    kw.setdefault("messages", [ChatMessage(role="user", content="write an essay")])
    return ChatRequest(**kw)  # type: ignore[arg-type]


class _Resp:
    status_code = 200
    headers: dict = {}
    text = ""

    def json(self) -> dict:
        return {
            "model": "m",
            "message": {"content": "hi"},
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
            "content": [{"type": "text", "text": "hi"}],
            "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


class _Client:
    captured: dict = {}

    def __init__(self, *a: object, **k: object) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a: object):
        return False

    async def aclose(self) -> None:
        return None

    async def post(self, url, json=None, headers=None, **kw):
        _Client.captured = json or {}
        return _Resp()


# --- every backend, under its own name ---------------------------------------


@pytest.mark.parametrize(
    ("provider", "api_key", "locate"),
    [
        (OpenAIProvider, "sk-x", lambda p: p.get("stop")),
        (AnthropicProvider, "sk-x", lambda p: p.get("stop_sequences")),
        (GeminiProvider, "sk-x", lambda p: p["generationConfig"].get("stopSequences")),
        (OllamaProvider, None, lambda p: p["options"].get("stop")),
    ],
)
async def test_stop_reaches_the_provider(monkeypatch, provider, api_key, locate) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await provider().chat(_req(stop=STOP), api_key=api_key)
    assert locate(_Client.captured) == STOP


@pytest.mark.parametrize(
    ("provider", "api_key", "container"),
    [
        (OpenAIProvider, "sk-x", lambda p: p),
        (AnthropicProvider, "sk-x", lambda p: p),
        (GeminiProvider, "sk-x", lambda p: p["generationConfig"]),
        (OllamaProvider, None, lambda p: p["options"]),
    ],
)
async def test_no_stop_sends_no_key(monkeypatch, provider, api_key, container) -> None:
    """Absent means absent — an empty `[]` is not the same request."""
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await provider().chat(_req(), api_key=api_key)
    sent = container(_Client.captured)
    assert not {"stop", "stop_sequences", "stopSequences"} & set(sent)


# --- normalization ------------------------------------------------------------


def test_a_bare_string_becomes_a_list() -> None:
    """OpenAI accepts `stop: "END"`; Anthropic, Gemini and Ollama all require an
    array. Widening once in the schema means no provider has to remember."""
    assert _req(stop="END").stop == ["END"]


@pytest.mark.parametrize("value", [[], "", ["", ""], None])
def test_an_empty_stop_is_dropped_entirely(value: object) -> None:
    """An empty string would stop generation immediately, and `[]` would be sent
    as a meaningless key."""
    assert _req(stop=value).stop is None


# --- the cache must not collide -----------------------------------------------


def test_stop_changes_the_cache_key() -> None:
    """cache_key's own docstring: "the request fields that affect the response"
    — two requests alike but for `stop` get different answers, so sharing an
    entry would replay the wrong one."""
    assert cache_key(_req(), "openai") != cache_key(_req(stop=STOP), "openai")
    assert cache_key(_req(stop=["A"]), "openai") != cache_key(_req(stop=["B"]), "openai")


def test_stop_changes_the_semantic_bucket() -> None:
    """The same reasoning, one layer out: a paraphrase may only be answered from
    an entry whose non-message fields match exactly."""
    a = semantic_bucket(_req(), "openai", "client-1")
    b = semantic_bucket(_req(stop=STOP), "openai", "client-1")
    assert a != b


# --- end to end ---------------------------------------------------------------


def test_openai_compatible_endpoint_forwards_stop(monkeypatch) -> None:
    """The path that made this a compatibility bug rather than a missing
    feature: `stop` was accepted by `extra="allow"` and discarded in
    translation."""
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    app = create_app(
        Settings(
            environment="test",
            default_provider="openai",
            cache_enabled=False,
            rate_limit_enabled=False,
        )
    )
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "list three lines"}],
            "stop": "\n",
        },
        headers={"X-Provider-Key": "sk-byok"},
    )
    assert resp.status_code == 200
    assert _Client.captured["stop"] == ["\n"]
