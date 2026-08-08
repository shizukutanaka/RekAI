"""Tests for the semantic response cache.

A semantic hit answers a prompt that was **never sent**, so these tests care
less about "does it hit" than about "does it hit only when it should": the right
bucket, the right tenant, a live entry, and an embedding that actually carries
meaning.
"""

from __future__ import annotations

import pytest

import rekai.semantic_cache as semantic_cache_module
from rekai.cache import NullCache, semantic_bucket
from rekai.config import Settings
from rekai.main import create_app
from rekai.providers import register_provider
from rekai.providers.base import EmbeddingResult, Provider, ProviderResult
from rekai.schemas import ChatMessage, ChatRequest, Usage
from rekai.semantic_cache import (
    SemanticCache,
    cosine_similarity,
    discriminators,
    semantic_cache,
)
from rekai.service import handle_chat


def test_cosine_similarity_basics() -> None:
    assert cosine_similarity([1, 0], [1, 0]) == 1.0
    assert cosine_similarity([1, 0], [0, 1]) == 0.0
    assert round(cosine_similarity([1, 1], [-1, -1]), 6) == -1.0
    assert cosine_similarity([1, 2], [1]) == 0.0  # mismatched length


def test_find_respects_threshold_and_bucket() -> None:
    sc = SemanticCache()
    sc.add("b1", "p", [1.0, 0.0], '{"r": 1}', ttl=60)
    # Identical vector -> sim 1.0 >= threshold -> hit, and find() hands back the
    # similarity alongside the payload so the caller can disclose it.
    assert sc.find("b1", "p", [1.0, 0.0], 0.85) == ('{"r": 1}', 1.0)
    # Orthogonal -> sim 0 < threshold -> miss.
    assert sc.find("b1", "p", [0.0, 1.0], 0.85) is None
    # A near vector above threshold still hits, with its own lower similarity.
    payload, similarity = sc.find("b1", "p", [0.99, 0.01], 0.85)
    assert payload == '{"r": 1}'
    assert 0.85 <= similarity < 1.0
    # Different bucket -> never matches.
    assert sc.find("b2", "p", [1.0, 0.0], 0.85) is None


def test_eviction_is_bounded_fifo() -> None:
    sc = SemanticCache(max_entries=2)
    sc.add("b", "p", [1.0, 0.0], "first", ttl=60)
    sc.add("b", "p", [0.0, 1.0], "second", ttl=60)
    sc.add("b", "p", [1.0, 1.0], "third", ttl=60)  # evicts "first"
    # "first" (exact [1,0]) is gone; [0,1] and [1,1] remain.
    assert sc.find("b", "p", [1.0, 0.0], 0.999) is None


# --- TTL ---------------------------------------------------------------------
# The exact cache honors REKAI_CACHE_TTL_SECONDS; a semantic entry that outlived
# the exact entry it mirrors would be the more surprising of the two to keep.


def test_entries_expire() -> None:
    sc = SemanticCache()
    sc.add("b", "p", [1.0, 0.0], "payload", ttl=60, now=1000.0)
    assert sc.find("b", "p", [1.0, 0.0], 0.85, now=1059.0) == ("payload", 1.0)
    assert sc.find("b", "p", [1.0, 0.0], 0.85, now=1060.0) is None


def test_zero_ttl_stores_nothing() -> None:
    sc = SemanticCache()
    sc.add("b", "p", [1.0, 0.0], "payload", ttl=0)
    assert sc.find("b", "p", [1.0, 0.0], 0.85) is None


def test_resize_applies_the_configured_bound() -> None:
    sc = SemanticCache(max_entries=1000)
    for i in range(5):
        sc.add("b", "p", [float(i), 1.0], f"e{i}", ttl=60)
    sc.resize(2)
    # Rebuilt with the cap, keeping the newest entries.
    assert len(sc._entries) == 2
    assert sc._entries.maxlen == 2
    assert [e[3] for e in sc._entries] == ["e3", "e4"]  # entry = (bucket, vec, marks, payload, exp)


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


# --- similarity computation --------------------------------------------------
# Vectors are unit-normalized on insert so a comparison is a bare dot product,
# and NumPy does that dot product when installed. Two representations means the
# suite has to prove they agree — the pure-Python path is the reference.


@pytest.fixture(params=["python", "numpy"])
def similarity_backend(request, monkeypatch):
    """Run a test under both the pure-Python and the NumPy path."""
    if request.param == "numpy":
        if semantic_cache_module.numpy is None:
            pytest.skip("numpy not installed")
    else:
        monkeypatch.setattr(semantic_cache_module, "numpy", None)
    return request.param


def test_similarity_matches_the_reference_cosine(similarity_backend) -> None:
    import random

    random.seed(11)
    a = [random.uniform(-1, 1) for _ in range(64)]
    b = [random.uniform(-1, 1) for _ in range(64)]
    prepared = semantic_cache_module._similarity(
        semantic_cache_module._prepare(a), semantic_cache_module._prepare(b)
    )
    # Normalizing first must not change the answer, only the cost of getting it.
    assert prepared == pytest.approx(cosine_similarity(a, b), abs=1e-12)


def test_identical_vectors_score_exactly_one(similarity_backend) -> None:
    # Summing float64 products leaves self-similarity at 0.9999999999999998, so
    # a threshold of exactly 1.0 ("only an identical embedding may hit") would
    # never match anything. Rounding above the noise floor fixes that; the clamp
    # covers the other direction, where a score above 1 would be nonsense and
    # could outrank a genuinely closer entry.
    vec = [0.3, -0.7, 0.2, 0.9]
    prepared = semantic_cache_module._prepare(vec)
    assert semantic_cache_module._similarity(prepared, prepared) == 1.0

    sc = SemanticCache()
    sc.add("b", "p", vec, "self", ttl=60)
    assert sc.find("b", "p", vec, threshold=1.0) == ("self", 1.0)


def test_zero_vector_scores_zero_rather_than_dividing_by_zero(similarity_backend) -> None:
    zero = semantic_cache_module._prepare([0.0, 0.0, 0.0])
    other = semantic_cache_module._prepare([1.0, 0.0, 0.0])
    assert semantic_cache_module._similarity(zero, other) == 0.0
    assert cosine_similarity([0.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == 0.0  # same as the reference


def test_mismatched_dimensions_never_match(similarity_backend) -> None:
    # Changing REKAI_SEMANTIC_CACHE_MODEL leaves entries from the old model in
    # this process-local store. Their coordinates mean nothing in the new
    # model's space. Scoring them 0.0 is not enough: with threshold 0.0 that
    # *is* a match (0.0 >= 0.0), so they have to be skipped outright.
    sc = SemanticCache()
    sc.add("b", "p", [1.0, 0.0, 0.0], "three-dim", ttl=60)
    assert sc.find("b", "p", [1.0, 0.0], 0.0) is None
    assert sc.find("b", "p", [1.0, 0.0, 0.0], 0.0) == ("three-dim", 1.0)  # right dim still hits


def test_both_backends_pick_the_same_entry(similarity_backend) -> None:
    import random

    random.seed(12)
    vectors = [[random.uniform(-1, 1) for _ in range(32)] for _ in range(20)]
    sc = SemanticCache()
    for i, vec in enumerate(vectors):
        sc.add("b", "p", vec, f"entry-{i}", ttl=60)
    hit = sc.find("b", "p", vectors[7], 0.0)
    assert hit is not None
    payload, similarity = hit
    assert payload == "entry-7"  # its own vector is the nearest
    assert similarity == pytest.approx(1.0)


def test_lookup_duration_is_recorded() -> None:
    from rekai.metrics import Metrics

    m = Metrics()
    m.observe_semantic_lookup("miss", 0.05)
    out = m.render()
    assert 'rekai_semantic_cache_lookup_seconds_count{result="miss"} 1' in out


# --- discriminator guard -----------------------------------------------------
# Cosine similarity is not proof that two prompts have the same answer, and it
# fails hardest on the small edits that flip meaning — exactly what a
# paraphrase-hunting cache is built to ignore. GPTCache (arXiv:2311.13133) adds
# a second model after the vector search for this; RekAI instead checks the two
# features that most reliably change an answer while barely moving an embedding.


@pytest.mark.parametrize(
    "stored,queried",
    [
        # Negation: near-identical vectors, opposite question.
        ("is aspirin safe during pregnancy", "is aspirin not safe during pregnancy"),
        ("can I deploy this on friday", "can I never deploy this on friday"),
        ("show the failing tests", "show the tests without failures"),
        # Numbers: entity/quantity substitution.
        ("convert 5 USD to EUR", "convert 500 USD to EUR"),
        ("summarize invoice 12345", "summarize invoice 12346"),
        ("retry 3 times", "retry 4 times"),
        # Order matters — the same multiset is a different question.
        ("convert 5 to 10", "convert 10 to 5"),
    ],
)
def test_meaning_flipping_edits_never_hit(stored: str, queried: str) -> None:
    # The embeddings are deliberately *identical*, so nothing but the guard can
    # prevent the hit. This is the case a similarity threshold cannot catch:
    # raising it to 1.0 would not help, because the vectors really are that close.
    sc = SemanticCache()
    sc.add("b", stored, [1.0, 0.0], "stored-answer", ttl=60)
    assert sc.find("b", queried, [1.0, 0.0], 0.85) is None
    # ...and the original prompt still hits, so the guard is not just breaking it.
    assert sc.find("b", stored, [1.0, 0.0], 0.85) == ("stored-answer", 1.0)


@pytest.mark.parametrize(
    "stored,queried",
    [
        ("how do i reset my password", "i forgot my password, help"),
        ("what is the capital of france", "france's capital city?"),
        ("retry 3 times", "please retry 3 times"),  # same number, still a hit
        ("do not retry", "don't retry"),  # n't counts as one negation, as does not
    ],
)
def test_true_paraphrases_still_hit(stored: str, queried: str) -> None:
    sc = SemanticCache()
    sc.add("b", stored, [1.0, 0.0], "stored-answer", ttl=60)
    assert sc.find("b", queried, [1.0, 0.0], 0.85) == ("stored-answer", 1.0)


def test_number_normalization_is_value_based() -> None:
    assert discriminators("retry 5 times") == discriminators("retry 5.0 times")
    assert discriminators("retry 5 times") != discriminators("retry 50 times")


def test_digest_is_all_that_is_retained_of_the_prompt() -> None:
    # The semantic cache must not become a place prompt text accumulates.
    sc = SemanticCache()
    secret_prompt = "my customer account number is 998877 and the passphrase is hunter2"
    sc.add("b", secret_prompt, [1.0, 0.0], "answer", ttl=60)
    stored = repr(list(sc._entries))
    assert "passphrase" not in stored
    assert "hunter2" not in stored
    # Numbers survive as a digest by design — that is what the guard compares.
    assert discriminators(secret_prompt) == (("998877.0", "2.0"), 0)


def test_guard_is_inert_on_prompts_with_neither_feature() -> None:
    # Most conversational traffic has no numbers and no negation, so the guard
    # costs those requests nothing in hit rate.
    assert discriminators("summarize this article about cats") == ((), 0)
    assert discriminators("write a haiku about the sea") == ((), 0)
