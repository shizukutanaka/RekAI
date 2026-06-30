"""Tests for the semantic response cache."""

from __future__ import annotations

from fastapi.testclient import TestClient

from rekai.config import Settings
from rekai.main import create_app
from rekai.semantic_cache import SemanticCache, cosine_similarity


def test_cosine_similarity_basics() -> None:
    assert cosine_similarity([1, 0], [1, 0]) == 1.0
    assert cosine_similarity([1, 0], [0, 1]) == 0.0
    assert round(cosine_similarity([1, 1], [-1, -1]), 6) == -1.0
    assert cosine_similarity([1, 2], [1]) == 0.0  # mismatched length


def test_find_respects_threshold_and_bucket() -> None:
    sc = SemanticCache()
    sc.add("b1", [1.0, 0.0], '{"r": 1}')
    # Identical vector -> sim 1.0 >= threshold -> hit.
    assert sc.find("b1", [1.0, 0.0], 0.85) == '{"r": 1}'
    # Orthogonal -> sim 0 < threshold -> miss.
    assert sc.find("b1", [0.0, 1.0], 0.85) is None
    # A near vector above threshold still hits.
    assert sc.find("b1", [0.99, 0.01], 0.85) == '{"r": 1}'
    # Different bucket -> never matches.
    assert sc.find("b2", [1.0, 0.0], 0.85) is None


def test_eviction_is_bounded_fifo() -> None:
    sc = SemanticCache(max_entries=2)
    sc.add("b", [1.0, 0.0], "first")
    sc.add("b", [0.0, 1.0], "second")
    sc.add("b", [1.0, 1.0], "third")  # evicts "first"
    # "first" (exact [1,0]) is gone; [0,1] and [1,1] remain.
    assert sc.find("b", [1.0, 0.0], 0.999) is None


def test_semantic_cache_hit_endpoint() -> None:
    # echo embeddings are deterministic, so identical prompts collide and the
    # second request is served from the semantic cache.
    # Content cache OFF so a hit can only come from the semantic cache.
    settings = Settings(
        environment="test",
        default_provider="echo",
        cache_enabled=False,
        semantic_cache_enabled=True,
        semantic_cache_model="echo",
    )
    client = TestClient(create_app(settings))
    from rekai.semantic_cache import semantic_cache

    semantic_cache.clear()
    body = {"model": "echo", "messages": [{"role": "user", "content": "hello"}]}
    first = client.post("/v1/chat", json=body)
    second = client.post("/v1/chat", json=body)
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True  # served from the semantic cache
    assert second.json()["content"] == first.json()["content"]
    semantic_cache.clear()
