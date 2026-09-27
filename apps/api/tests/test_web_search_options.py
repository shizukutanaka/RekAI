"""`web_search_options` and `stream_options.include_obfuscation` forwarding.

Both are OpenAI request fields the compat layer tolerated via ``extra="allow"``
and dropped. Hosted web search changes what the answer is grounded on; token
obfuscation changes the streamed encoding (a security feature for agents
echoing untrusted content). Neither has an Anthropic/Gemini/Ollama
counterpart — they reach OpenAI-compatible providers only.
"""

from __future__ import annotations

import httpx

from rekai.cache import cache_key, semantic_bucket
from rekai.openai_compat import to_chat_request
from rekai.providers.openai import OpenAIProvider
from rekai.schemas import ChatCompletionsRequest, ChatMessage, ChatRequest, StreamOptions

_WSO = {"search_context_size": "low", "user_location": {"type": "approximate"}}


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
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
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


async def test_web_search_options_reach_openai(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(web_search_options=_WSO), api_key="sk-x")
    assert _Client.captured["web_search_options"] == _WSO


async def test_no_web_search_options_sends_no_key(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(), api_key="sk-x")
    assert "web_search_options" not in _Client.captured


async def test_include_obfuscation_merges_into_stream_options(monkeypatch) -> None:
    """The flag must merge with the include_usage RekAI always asks for, not
    replace it."""
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    payload = OpenAIProvider()._build_payload(_req(include_obfuscation=True), stream=True)
    assert payload["stream_options"] == {"include_usage": True, "include_obfuscation": True}


def test_compat_maps_both() -> None:
    req = ChatCompletionsRequest(
        model="gpt-5",
        messages=[{"role": "user", "content": "hi"}],
        web_search_options=_WSO,
        stream_options=StreamOptions(include_usage=True, include_obfuscation=True),
    )
    chat = to_chat_request(req)
    assert chat.web_search_options == _WSO
    assert chat.include_obfuscation is True


def test_web_search_options_change_the_cache_key() -> None:
    assert cache_key(_req(), "openai") != cache_key(_req(web_search_options=_WSO), "openai")


def test_web_search_options_change_the_semantic_bucket() -> None:
    a = semantic_bucket(_req(), "openai", "client-1")
    b = semantic_bucket(_req(web_search_options=_WSO), "openai", "client-1")
    assert a != b


def test_include_obfuscation_does_not_change_the_cache_key() -> None:
    """Obfuscation only scrambles the streamed encoding — a cached body is
    never streamed, so the flag must not partition the cache."""
    assert cache_key(_req(), "openai") == cache_key(_req(include_obfuscation=True), "openai")
