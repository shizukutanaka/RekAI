"""`prediction`/`store`/`metadata` must reach OpenAI-compatible providers.

`prediction` (Predicted Outputs) lets the model speculatively reuse known
content — a real latency feature for edit-style tasks. `store`/`metadata`
are upstream bookkeeping used by evals/distillation pipelines. All three
were tolerated via ``extra="allow"`` and silently dropped.
"""

from __future__ import annotations

import httpx
import pytest

from rekai.cache import cache_key, semantic_bucket
from rekai.openai_compat import to_chat_request
from rekai.providers.anthropic import AnthropicProvider
from rekai.providers.gemini import GeminiProvider
from rekai.providers.ollama import OllamaProvider
from rekai.providers.openai import OpenAIProvider
from rekai.schemas import ChatCompletionsRequest, ChatMessage, ChatRequest

_PRED = {"type": "content", "content": "the expected answer"}


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


async def test_extras_reach_openai(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(
        _req(prediction=_PRED, store=True, metadata={"job": "evals"}), api_key="sk-x"
    )
    assert _Client.captured["prediction"] == _PRED
    assert _Client.captured["store"] is True
    assert _Client.captured["metadata"] == {"job": "evals"}


async def test_unset_sends_no_keys(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(), api_key="sk-x")
    assert not {"prediction", "store", "metadata"} & set(_Client.captured)


@pytest.mark.parametrize(
    ("provider", "api_key"),
    [
        (AnthropicProvider, "sk-x"),
        (GeminiProvider, "sk-x"),
        (OllamaProvider, None),
    ],
)
async def test_extras_not_sent_to_others(monkeypatch, provider, api_key) -> None:
    """Anthropic/Gemini/Ollama have no such fields — they must not leak."""
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await provider().chat(_req(prediction=_PRED, store=True, metadata={"a": "b"}), api_key=api_key)
    assert not {"prediction", "store", "metadata"} & set(_Client.captured)


def test_prediction_changes_the_cache_key() -> None:
    """Predicted outputs change what gets generated — two requests differing
    only in prediction must not share an entry."""
    assert cache_key(_req(), "openai") != cache_key(_req(prediction=_PRED), "openai")


def test_prediction_changes_the_semantic_bucket() -> None:
    a = semantic_bucket(_req(), "openai", "client-1")
    b = semantic_bucket(_req(prediction=_PRED), "openai", "client-1")
    assert a != b


def test_store_metadata_do_not_change_the_cache_key() -> None:
    """Bookkeeping must not fragment the cache — the response is identical."""
    base = cache_key(_req(), "openai")
    assert cache_key(_req(store=True, metadata={"a": "b"}), "openai") == base


def test_compat_maps_extras() -> None:
    req = ChatCompletionsRequest(
        model="gpt-5",
        messages=[{"role": "user", "content": "hi"}],
        prediction=_PRED,
        store=False,
        metadata={"team": "search"},
    )
    out = to_chat_request(req)
    assert out.prediction == _PRED
    assert out.store is False
    assert out.metadata == {"team": "search"}
