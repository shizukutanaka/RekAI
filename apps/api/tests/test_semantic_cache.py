"""Tests for the semantic response cache.

A semantic hit answers a prompt that was **never sent**, so these tests care
less about "does it hit" than about "does it hit only when it should": the right
bucket, the right tenant, a live entry, and an embedding that actually carries
meaning.
"""

from __future__ import annotations

import pytest

from rekai.cache import NullCache, semantic_bucket
from rekai.config import Settings
from rekai.main import create_app
from rekai.providers import register_provider
from rekai.providers.base import EmbeddingResult, Provider, ProviderResult
from rekai.schemas import ChatMessage, ChatRequest, Usage
from rekai.semantic_cache import SemanticCache, cosine_similarity, semantic_cache
from rekai.service import handle_chat


def test_cosine_similarity_basics() -> None:
    assert cosine_similarity([1, 0], [1, 0]) == 1.0
    assert cosine_similarity([1, 0], [0, 1]) == 0.0
    assert round(cosine_similarity([1, 1], [-1, -1]), 6) == -1.0
    assert cosine_similarity([1, 2], [1]) == 0.0  # mismatched length


def test_find_respects_threshold_and_bucket() -> None:
    sc = SemanticCache()
    sc.add("b1", [1.0, 0.0], '{"r": 1}', ttl=60)
    # Identical vector -> sim 1.0 >= threshold -> hit, and find() hands back the
    # similarity alongside the payload so the caller can disclose it.
    assert sc.find("b1", [1.0, 0.0], 0.85) == ('{"r": 1}', 1.0)
    # Orthogonal -> sim 0 < threshold -> miss.
    assert sc.find("b1", [0.0, 1.0], 0.85) is None
    # A near vector above threshold still hits, with its own lower similarity.
    payload, similarity = sc.find("b1", [0.99, 0.01], 0.85)
    assert payload == '{"r": 1}'
    assert 0.85 <= similarity < 1.0
    # Different bucket -> never matches.
    assert sc.find("b2", [1.0, 0.0], 0.85) is None


def test_eviction_is_bounded_fifo() -> None:
    sc = SemanticCache(max_entries=2)
    sc.add("b", [1.0, 0.0], "first", ttl=60)
    sc.add("b", [0.0, 1.0], "second", ttl=60)
    sc.add("b", [1.0, 1.0], "third", ttl=60)  # evicts "first"
    # "first" (exact [1,0]) is gone; [0,1] and [1,1] remain.
    assert sc.find("b", [1.0, 0.0], 0.999) is None


# --- TTL ---------------------------------------------------------------------
# The exact cache honors REKAI_CACHE_TTL_SECONDS; a semantic entry that outlived
# the exact entry it mirrors would be the more surprising of the two to keep.


def test_entries_expire() -> None:
    sc = SemanticCache()
    sc.add("b", [1.0, 0.0], "payload", ttl=60, now=1000.0)
    assert sc.find("b", [1.0, 0.0], 0.85, now=1059.0) == ("payload", 1.0)
    assert sc.find("b", [1.0, 0.0], 0.85, now=1060.0) is None


def test_zero_ttl_stores_nothing() -> None:
    sc = SemanticCache()
    sc.add("b", [1.0, 0.0], "payload", ttl=0)
    assert sc.find("b", [1.0, 0.0], 0.85) is None


def test_resize_applies_the_configured_bound() -> None:
    sc = SemanticCache(max_entries=1000)
    for i in range(5):
        sc.add("b", [float(i), 1.0], f"e{i}", ttl=60)
    sc.resize(2)
    # Rebuilt with the cap, keeping the newest entries.
    assert len(sc._entries) == 2
    assert sc._entries.maxlen == 2
    assert [e[2] for e in sc._entries] == ["e3", "e4"]


def test_create_app_applies_max_entries() -> None:
    create_app(
        Settings(
            environment="test",
            default_provider="echo",
            semantic_cache_max_entries=7,
        )
    )
    assert semantic_cache._entries.maxlen == 7
    semantic_cache.resize(1000)  # restore the default for other tests


# --- bucketing ---------------------------------------------------------------
# The bucket used to be f"{provider}:{model}:{temperature}:{max_tokens}", which
# reproduced exactly the collisions cache_key documents guarding against.


def _req(**kw) -> ChatRequest:
    kw.setdefault("model", "gpt-4o")
    kw.setdefault("messages", [ChatMessage(role="user", content="hi")])
    return ChatRequest(**kw)


@pytest.mark.parametrize(
    "differing",
    [
        {"response_format": {"type": "json_object"}},
        {"tools": [{"type": "function", "function": {"name": "f"}}]},
        {"tool_choice": "required"},
        {"cache_control": {"type": "ephemeral"}},
        {"temperature": 0.1},
        {"max_tokens": 16},
        {"model": "gpt-4o-mini"},
    ],
)
def test_bucket_separates_requests_that_must_not_share_an_answer(differing: dict) -> None:
    base = semantic_bucket(_req(), "openai", "key:aaa")
    assert semantic_bucket(_req(**differing), "openai", "key:aaa") != base


def test_bucket_separates_clients() -> None:
    # A semantic hit answers a question the caller never asked, so it must not
    # cross tenants — unlike the exact cache, where a hit means the caller sent
    # the identical prompt itself.
    assert semantic_bucket(_req(), "openai", "key:aaa") != semantic_bucket(
        _req(), "openai", "key:bbb"
    )


def test_bucket_ignores_message_text() -> None:
    # The text is what the embedding compares; it must not partition the bucket.
    other = _req(messages=[ChatMessage(role="user", content="a completely different prompt")])
    assert semantic_bucket(other, "openai", "key:aaa") == semantic_bucket(
        _req(), "openai", "key:aaa"
    )


# --- end to end --------------------------------------------------------------


class StubSemanticProvider(Provider):
    """Chat + embeddings with hand-controlled vectors.

    The point is to make "paraphrase" and "unrelated" mean something: prompts
    are mapped to orthogonal-ish vectors by topic, so a hit proves similarity
    matching rather than proving two identical strings hash the same (which is
    all an echo-backed test could ever show).
    """

    name = "semstub"
    requires_key = False
    _VECTORS = {
        "how do i reset my password": [1.0, 0.0, 0.0],
        "i forgot my password, help": [0.98, 0.2, 0.0],  # paraphrase
        "what is the capital of france": [0.0, 1.0, 0.0],  # unrelated
    }

    def __init__(self) -> None:
        super().__init__()
        self.chat_calls = 0

    async def chat(self, request, api_key) -> ProviderResult:
        self.chat_calls += 1
        text = request.messages[-1].content or ""
        return ProviderResult(content=f"answer to {text}", model=request.model, usage=Usage())

    async def stream(self, request, api_key):  # pragma: no cover - unused here
        yield ""

    async def embed(self, inputs, model, api_key) -> EmbeddingResult:
        return EmbeddingResult(
            embeddings=[self._VECTORS[t] for t in inputs], model=model, usage=Usage()
        )

    async def list_models(self, api_key) -> list[str]:
        return ["semstub"]

    async def list_embedding_models(self, api_key) -> list[str]:
        return ["semstub"]


def _semantic_settings(**kw) -> Settings:
    return Settings(
        environment="test",
        default_provider="semstub",
        cache_enabled=False,  # a hit can then only come from the semantic cache
        semantic_cache_enabled=True,
        semantic_cache_model="semstub",
        **kw,
    )


async def test_paraphrase_hits_and_unrelated_prompt_misses() -> None:
    provider = StubSemanticProvider()
    register_provider(provider)
    semantic_cache.clear()
    settings = _semantic_settings()

    def ask(text: str) -> ChatRequest:
        return ChatRequest(model="semstub", messages=[ChatMessage(role="user", content=text)])

    first = await handle_chat(ask("how do i reset my password"), None, settings, NullCache(), "c1")
    assert first.cached is False
    assert provider.chat_calls == 1

    # A paraphrase (cos ≈ 0.98) is served from the semantic cache.
    para = await handle_chat(ask("i forgot my password, help"), None, settings, NullCache(), "c1")
    assert para.cached is True
    assert para.content == first.content
    assert provider.chat_calls == 1  # no upstream call

    # An unrelated prompt (cos 0.0) must not be.
    other = await handle_chat(
        ask("what is the capital of france"), None, settings, NullCache(), "c1"
    )
    assert other.cached is False
    assert provider.chat_calls == 2
    semantic_cache.clear()


async def test_a_paraphrase_from_another_client_does_not_hit() -> None:
    provider = StubSemanticProvider()
    register_provider(provider)
    semantic_cache.clear()
    settings = _semantic_settings()

    def ask(text: str) -> ChatRequest:
        return ChatRequest(model="semstub", messages=[ChatMessage(role="user", content=text)])

    await handle_chat(ask("how do i reset my password"), None, settings, NullCache(), "key:aaa")
    # Same paraphrase, different tenant: it gets its own upstream call, not an
    # answer to a question only the first tenant asked.
    other = await handle_chat(
        ask("i forgot my password, help"), None, settings, NullCache(), "key:bbb"
    )
    assert other.cached is False
    assert provider.chat_calls == 2
    semantic_cache.clear()


# --- startup guards ----------------------------------------------------------


def test_enabling_without_a_model_is_refused_at_startup() -> None:
    with pytest.raises(ValueError, match="REKAI_SEMANTIC_CACHE_MODEL"):
        create_app(
            Settings(environment="test", default_provider="echo", semantic_cache_enabled=True)
        )


def test_echo_backed_semantic_cache_warns(capsys) -> None:
    # echo "embeddings" are a 16-dimension hash: every vector lands in the
    # positive orthant, unrelated prompts sit near 0.78 cosine, and a large
    # share of pairs clear the 0.85 default. Usable in tests, never in prod.
    # (capsys, not caplog: configure_logging installs its own stdout handler.)
    create_app(
        Settings(
            environment="test",
            default_provider="echo",
            semantic_cache_enabled=True,
            semantic_cache_model="echo",
        )
    )
    assert "echo provider" in capsys.readouterr().out


# --- disclosure --------------------------------------------------------------
# A semantic hit and an exact hit both arrive as cached=true. They are not the
# same claim: one is the answer to this prompt, the other to a different one.


async def test_semantic_hit_discloses_its_similarity() -> None:
    provider = StubSemanticProvider()
    register_provider(provider)
    semantic_cache.clear()
    settings = _semantic_settings()

    def ask(text: str) -> ChatRequest:
        return ChatRequest(model="semstub", messages=[ChatMessage(role="user", content=text)])

    fresh = await handle_chat(ask("how do i reset my password"), None, settings, NullCache(), "c1")
    assert fresh.cache_similarity is None  # a miss makes no similarity claim

    hit = await handle_chat(ask("i forgot my password, help"), None, settings, NullCache(), "c1")
    assert hit.cached is True
    assert hit.cache_similarity is not None
    assert 0.9 < hit.cache_similarity < 1.0  # the stub's paraphrase vector
    semantic_cache.clear()


def test_exact_cache_hit_makes_no_similarity_claim() -> None:
    # cache_similarity must stay null on an exact hit — a non-null value is
    # precisely the signal that an approximate match was used.
    from fastapi.testclient import TestClient

    client = TestClient(
        create_app(Settings(environment="test", default_provider="echo", rate_limit_enabled=False))
    )
    body = {"model": "echo", "messages": [{"role": "user", "content": "disclosure-exact"}]}
    client.post("/v1/chat", json=body)
    second = client.post("/v1/chat", json=body)
    assert second.json()["cached"] is True
    assert second.json()["cache_similarity"] is None
    assert "X-Cache-Similarity" not in second.headers


def test_semantic_hits_are_counted_separately() -> None:
    from rekai.metrics import Metrics

    m = Metrics()
    m.record_cache(hit=True)  # exact
    m.record_cache(hit=True)  # semantic...
    m.record_semantic_cache_hit()  # ...counted again in its own subset
    out = m.render()
    assert "rekai_cache_hits_total 2" in out
    assert "rekai_semantic_cache_hits_total 1" in out
    assert m.snapshot()["semantic_cache_hits_total"] == 1
