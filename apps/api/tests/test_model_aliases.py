"""Tests for REKAI_MODEL_ALIASES — virtual model names over a weighted pool."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rekai.config import Settings
from rekai.main import create_app
from rekai.router import expand_alias
from rekai.schemas import ChatMessage, ChatRequest


def _settings(**over) -> Settings:
    kw = {
        "environment": "test",
        "default_provider": "echo",
        "model_aliases": "fast=echo:echo@1,echo:echo-alt@2;premium=echo:echo-pro",
    }
    kw.update(over)
    return Settings(**kw)


def _request(model: str = "fast", **over) -> ChatRequest:
    return ChatRequest(model=model, messages=[ChatMessage(role="user", content="hi")], **over)


def test_alias_map_parses() -> None:
    m = _settings().model_alias_map
    assert m == {
        "fast": [("echo", "echo", 1), ("echo", "echo-alt", 2)],
        "premium": [("echo", "echo-pro", 1)],
    }


def test_non_alias_is_untouched() -> None:
    req = _request(model="echo")
    expand_alias(req, _settings())
    assert req.model == "echo"
    assert req.provider is None
    assert req.fallbacks is None


def test_alias_rewrites_to_pool_member() -> None:
    req = _request()
    expand_alias(req, _settings())
    assert req.provider == "echo"
    assert req.model in {"echo", "echo-alt"}
    # The rest of the pool becomes the implicit fallback chain.
    others = {"echo", "echo-alt"} - {req.model}
    assert [(f.provider, f.model) for f in req.fallbacks or []] == [("echo", next(iter(others)))]


def test_provider_pin_narrows_the_pool() -> None:
    req = _request(provider="nope")
    with pytest.raises(Exception) as exc:
        expand_alias(req, _settings(model_aliases="fast=echo:m1"))
    assert "no target on provider" in str(exc.value).lower()


def test_explicit_fallbacks_win_over_pool() -> None:
    from rekai.schemas import FallbackTarget

    req = _request(fallbacks=[FallbackTarget(provider="echo", model="mine")])
    expand_alias(req, _settings())
    assert [(f.provider, f.model) for f in req.fallbacks] == [("echo", "mine")]


def test_weights_bias_the_pick() -> None:
    seen = set()
    for _ in range(60):
        req = _request()
        expand_alias(req, _settings())
        seen.add(req.model)
    assert seen == {"echo", "echo-alt"}  # both eventually picked


def test_chat_route_end_to_end() -> None:
    app = create_app(_settings())
    with TestClient(app) as c:
        resp = c.post(
            "/v1/chat",
            json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        assert resp.json()["model"] in {"echo", "echo-alt"}


def test_alias_appears_in_models_listing() -> None:
    app = create_app(_settings())
    with TestClient(app) as c:
        data = c.get("/v1/models").json()["data"]
    alias = next((m for m in data if m["id"] == "fast"), None)
    assert alias is not None and alias["provider"] == "alias" and alias["type"] == "chat"
    # ...and is filtered out of the embedding view.
    data = c.get("/v1/models?type=embedding").json()["data"]
    assert all(m["id"] != "fast" for m in data)


def test_embeddings_route_expands_alias() -> None:
    app = create_app(_settings(model_aliases="embed=echo:echo", custom_embedding_models="echo"))
    with TestClient(app) as c:
        resp = c.post("/v1/embeddings", json={"model": "embed", "input": "hello"})
        assert resp.status_code == 200
        assert resp.json()["model"] == "echo"
