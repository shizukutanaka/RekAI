"""OpenAI Predicted Outputs (`prediction`) forwarding and cache identity."""

from __future__ import annotations

import httpx

from rekai.cache import cache_key, semantic_bucket
from rekai.openai_compat import to_chat_request
from rekai.providers.anthropic import AnthropicProvider
from rekai.providers.openai import OpenAIProvider
from rekai.schemas import ChatCompletionsRequest, ChatMessage, ChatRequest

_PREDICTION = {"type": "content", "content": "const answer = 42;"}


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


def test_prediction_maps_to_chat_request() -> None:
    req = ChatCompletionsRequest(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        prediction=_PREDICTION,
    )
    assert to_chat_request(req).prediction == _PREDICTION


async def test_prediction_reaches_openai(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_req(prediction=_PREDICTION), api_key="sk-x")
    assert _Client.captured["prediction"] == _PREDICTION


async def test_prediction_not_sent_to_anthropic(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await AnthropicProvider().chat(_req(prediction=_PREDICTION), api_key="sk-x")
    assert "prediction" not in _Client.captured


def test_prediction_changes_the_cache_key() -> None:
    assert cache_key(_req(), "openai") != cache_key(_req(prediction=_PREDICTION), "openai")


def test_prediction_changes_the_semantic_bucket() -> None:
    a = semantic_bucket(_req(), "openai", "client-1")
    b = semantic_bucket(_req(prediction=_PREDICTION), "openai", "client-1")
    assert a != b
