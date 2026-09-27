"""OpenAI tuning params (`top_p`, `seed`, `frequency_penalty`, `presence_penalty`,
`logit_bias`) must reach the providers that support them, and must key the cache.

They were accepted on the OpenAI-compatible surface via `extra="allow"` and
then *dropped* — a caller asking for `seed=42` got a 200 that silently ignored
it. Each provider forwards only what it natively supports: OpenAI-compatible
backends take all five, Anthropic and Gemini take `top_p` (under their own
names), Ollama takes `top_p` and `seed`.
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


_REQ = _req(
    top_p=0.9,
    seed=42,
    frequency_penalty=0.5,
    presence_penalty=-0.5,
    logit_bias={"1234": -10},
)


async def test_openai_forwards_every_tuning_param(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await OpenAIProvider().chat(_REQ, api_key="sk-x")
    sent = _Client.captured
    assert sent["top_p"] == 0.9
    assert sent["seed"] == 42
    assert sent["frequency_penalty"] == 0.5
    assert sent["presence_penalty"] == -0.5
    assert sent["logit_bias"] == {"1234": -10}


@pytest.mark.parametrize(
    ("provider", "api_key", "expected", "absent"),
    [
        (
            AnthropicProvider,
            "sk-x",
            ("top_p", 0.9),
            {"seed", "frequency_penalty", "presence_penalty", "logit_bias"},
        ),
        (
            OllamaProvider,
            None,
            ("top_p", 0.9),
            {"frequency_penalty", "presence_penalty", "logit_bias"},
        ),
    ],
)
async def test_other_providers_forward_what_they_support(
    monkeypatch, provider, api_key, expected, absent
) -> None:
    """Anthropic has top_p but no seed/penalty/logit_bias fields; Ollama takes
    top_p + seed via `options`. The rest stay RekAI-side rather than erroring
    upstream."""
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await provider().chat(_REQ, api_key=api_key)
    sent = _Client.captured
    container = sent.get("options", sent) if provider is OllamaProvider else sent
    key, value = expected
    assert container[key] == value
    if provider is OllamaProvider:
        assert container["seed"] == 42
    for field in absent:
        assert field not in sent and field not in container


async def test_gemini_forwards_top_p_as_topP(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await GeminiProvider().chat(_REQ, api_key="sk-x")
    assert _Client.captured["generationConfig"]["topP"] == 0.9
    assert "seed" not in _Client.captured["generationConfig"]


@pytest.mark.parametrize(
    ("provider", "api_key", "container"),
    [
        (OpenAIProvider, "sk-x", lambda p: p),
        (AnthropicProvider, "sk-x", lambda p: p),
        (GeminiProvider, "sk-x", lambda p: p["generationConfig"]),
        (OllamaProvider, None, lambda p: p["options"]),
    ],
)
async def test_no_tuning_params_sends_no_keys(monkeypatch, provider, api_key, container) -> None:
    """Absent means absent — unset params must not appear in the payload."""
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await provider().chat(_req(), api_key=api_key)
    sent = container(_Client.captured)
    assert not {
        "top_p",
        "topP",
        "seed",
        "frequency_penalty",
        "presence_penalty",
        "logit_bias",
    } & set(sent)


# --- the cache must not collide -----------------------------------------------


def test_tuning_params_change_the_cache_key() -> None:
    """Different sampling parameters produce different answers — sharing a cache
    entry would replay the wrong one."""
    base = cache_key(_req(), "openai")
    for kw in (
        {"top_p": 0.9},
        {"seed": 42},
        {"frequency_penalty": 0.5},
        {"presence_penalty": 0.5},
        {"logit_bias": {"1": 1}},
    ):
        assert cache_key(_req(**kw), "openai") != base, kw


def test_tuning_params_change_the_semantic_bucket() -> None:
    a = semantic_bucket(_req(), "openai", "client-1")
    b = semantic_bucket(_req(seed=42), "openai", "client-1")
    assert a != b


# --- end to end ---------------------------------------------------------------


def test_openai_compatible_endpoint_forwards_tuning_params(monkeypatch) -> None:
    """The compat surface declared the fields typed, so they translate into
    ChatRequest instead of being dropped by `extra="allow"`."""
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
            "messages": [{"role": "user", "content": "hi"}],
            "top_p": 0.9,
            "seed": 42,
            "frequency_penalty": 0.5,
        },
        headers={"X-Provider-Key": "sk-byok"},
    )
    assert resp.status_code == 200
    assert _Client.captured["top_p"] == 0.9
    assert _Client.captured["seed"] == 42
    assert _Client.captured["frequency_penalty"] == 0.5
