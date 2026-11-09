"""The end-user id must reach providers that take one, under their own name.

OpenAI takes ``user`` and Anthropic takes ``metadata.user_id`` — both feed the
provider's abuse detection. RekAI accepted ``user`` and silently dropped it, so
a gateway caller's per-end-user tracking never reached the provider. Gemini and
Ollama have no such field — it must not reach them.

``user`` is a routing/billing hint, not a response-shaping field, so it does
not join the cache key (a cached answer is equally valid for any caller).
"""

from __future__ import annotations

import httpx

from rekai.anthropic_compat import to_chat_request as anthropic_to_chat_request
from rekai.cache import cache_key, semantic_bucket
from rekai.openai_compat import to_chat_request
from rekai.providers.anthropic import AnthropicProvider
from rekai.providers.gemini import GeminiProvider
from rekai.providers.ollama import OllamaProvider
from rekai.providers.openai import OpenAIProvider
from rekai.schemas import (
    AnthropicMessagesRequest,
    ChatCompletionsRequest,
    ChatMessage,
    ChatRequest,
)


def _req(**kw: object) -> ChatRequest:
    kw.setdefault("model", "m")
    kw.setdefault("messages", [ChatMessage(role="user", content="hi")])
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


async def test_user_reaches_openai_verbatim(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(user="u-123"), api_key="sk-x")
    assert _Client.captured["user"] == "u-123"


async def test_user_reaches_anthropic_as_metadata(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await AnthropicProvider().chat(_req(user="u-123"), api_key="sk-x")
    assert _Client.captured["metadata"] == {"user_id": "u-123"}


async def test_user_not_sent_to_gemini_or_ollama(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await GeminiProvider().chat(_req(user="u-123"), api_key="sk-x")
    gemini = _Client.captured
    await OllamaProvider().chat(_req(user="u-123"), api_key=None)
    ollama = _Client.captured
    assert "user" not in gemini and "metadata" not in gemini
    assert "user" not in ollama and "metadata" not in ollama


async def test_no_user_sends_no_key(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(), api_key="sk-x")
    assert "user" not in _Client.captured
    await AnthropicProvider().chat(_req(), api_key="sk-x")
    assert "metadata" not in _Client.captured


def test_user_does_not_change_the_cache_key() -> None:
    assert cache_key(_req(user="u-1"), "openai") == cache_key(_req(user="u-2"), "openai")


def test_user_does_not_change_the_semantic_bucket() -> None:
    a = semantic_bucket(_req(user="u-1"), "openai", "client-1")
    b = semantic_bucket(_req(user="u-2"), "openai", "client-1")
    assert a == b


def test_openai_compat_maps_user() -> None:
    req = ChatCompletionsRequest(
        model="m", messages=[{"role": "user", "content": "hi"}], user="u-9"
    )
    assert to_chat_request(req).user == "u-9"


def test_anthropic_compat_maps_metadata_user_id() -> None:
    req = AnthropicMessagesRequest(
        model="m",
        max_tokens=16,
        messages=[{"role": "user", "content": "hi"}],
        metadata={"user_id": "u-9"},
    )
    assert anthropic_to_chat_request(req).user == "u-9"


# --- embeddings --------------------------------------------------------------
# OpenAI's embeddings API takes the same `user` end-user id; the request model
# used to 422 on it (no extra="allow" there), and even a tolerated field would
# have stopped at the provider boundary.


async def test_user_reaches_openai_embeddings(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().embed(["hi"], "m", "sk-x", user="u-123")
    assert _Client.captured["user"] == "u-123"


async def test_no_user_sends_no_embeddings_key(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().embed(["hi"], "m", "sk-x")
    assert "user" not in _Client.captured


def test_embeddings_request_accepts_user() -> None:
    from rekai.schemas import EmbeddingsRequest

    assert EmbeddingsRequest(model="m", input="hi", user="u-1").user == "u-1"


# --- safety_identifier -------------------------------------------------------
# OpenAI's newer abuse-detection field (a hashed end-user handle meant to
# supersede `user`). OpenAI-compatible providers take it; Anthropic/Gemini/
# Ollama have no equivalent and must not receive it.


async def test_safety_identifier_reaches_openai(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(safety_identifier="hash-7"), api_key="sk-x")
    assert _Client.captured["safety_identifier"] == "hash-7"


async def test_safety_identifier_not_sent_to_anthropic(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await AnthropicProvider().chat(_req(safety_identifier="hash-7"), api_key="sk-x")
    assert "safety_identifier" not in _Client.captured


def test_openai_compat_maps_safety_identifier() -> None:
    req = ChatCompletionsRequest(
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        safety_identifier="hash-9",
    )
    assert to_chat_request(req).safety_identifier == "hash-9"


# --- prompt_cache_key --------------------------------------------------------
# OpenAI's caller-chosen prompt-cache bucket. OpenAI-compatible providers take
# it; other providers have no equivalent and must not receive it. Like `user`,
# it steers upstream cache routing rather than the answer, so it stays out of
# RekAI's exact and semantic cache keys.


async def test_prompt_cache_key_reaches_openai(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(prompt_cache_key="tenant-a"), api_key="sk-x")
    assert _Client.captured["prompt_cache_key"] == "tenant-a"


async def test_prompt_cache_key_not_sent_to_anthropic(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await AnthropicProvider().chat(_req(prompt_cache_key="tenant-a"), api_key="sk-x")
    assert "prompt_cache_key" not in _Client.captured


def test_openai_compat_maps_prompt_cache_key() -> None:
    req = ChatCompletionsRequest(
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        prompt_cache_key="tenant-a",
    )
    assert to_chat_request(req).prompt_cache_key == "tenant-a"


def test_prompt_cache_key_does_not_change_the_cache_key() -> None:
    assert cache_key(_req(prompt_cache_key="a"), "openai") == cache_key(
        _req(prompt_cache_key="b"), "openai"
    )


def test_prompt_cache_key_does_not_change_the_semantic_bucket() -> None:
    a = semantic_bucket(_req(prompt_cache_key="a"), "openai", "client-1")
    b = semantic_bucket(_req(prompt_cache_key="b"), "openai", "client-1")
    assert a == b


# --- prompt_cache_options / prompt_cache_retention ---------------------------
# OpenAI's newer breakpoint/TTL config and the deprecated retention spelling.
# They change upstream cache behavior and write pricing rather than the prompt,
# but a cached RekAI response also stores its measured cost, so these join the
# exact/semantic keys.


async def test_prompt_cache_options_reach_openai(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    options = {"mode": "explicit", "ttl": "12h"}
    await OpenAIProvider().chat(
        _req(prompt_cache_options=options, prompt_cache_retention="24h"), api_key="sk-x"
    )
    assert _Client.captured["prompt_cache_options"] == options
    assert _Client.captured["prompt_cache_retention"] == "24h"


async def test_prompt_cache_options_not_sent_to_anthropic(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await AnthropicProvider().chat(
        _req(
            prompt_cache_options={"mode": "explicit", "ttl": "12h"},
            prompt_cache_retention="24h",
        ),
        api_key="sk-x",
    )
    assert "prompt_cache_options" not in _Client.captured
    assert "prompt_cache_retention" not in _Client.captured


def test_openai_compat_maps_prompt_cache_options() -> None:
    options = {"mode": "explicit", "ttl": "12h"}
    req = ChatCompletionsRequest(
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        prompt_cache_options=options,
        prompt_cache_retention="24h",
    )
    chat = to_chat_request(req)
    assert chat.prompt_cache_options == options
    assert chat.prompt_cache_retention == "24h"


def test_prompt_cache_options_change_the_cache_key() -> None:
    options = {"mode": "explicit", "ttl": "12h"}
    assert cache_key(_req(), "openai") != cache_key(_req(prompt_cache_options=options), "openai")
    assert cache_key(_req(), "openai") != cache_key(_req(prompt_cache_retention="24h"), "openai")


def test_prompt_cache_options_change_the_semantic_bucket() -> None:
    options = {"mode": "explicit", "ttl": "12h"}
    a = semantic_bucket(_req(), "openai", "client-1")
    b = semantic_bucket(_req(prompt_cache_options=options), "openai", "client-1")
    c = semantic_bucket(_req(prompt_cache_retention="24h"), "openai", "client-1")
    assert a != b
    assert a != c
