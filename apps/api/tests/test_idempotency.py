"""Tests for Idempotency-Key semantics on the chat/embeddings/moderations
endpoints and the idempotency module (claim / complete / release lifecycle)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from rekai import auth, idempotency
from rekai.cache import MemoryCache
from rekai.config import Settings
from rekai.main import create_app
from rekai.openai_compat import to_chat_request
from rekai.providers.base import ProviderError
from rekai.providers.echo import EchoProvider
from rekai.schemas import ChatCompletionsRequest, ChatRequest, EmbeddingsRequest


def _client() -> TestClient:
    # Memory cache (default) backs the idempotency store.
    return TestClient(create_app(Settings(environment="test", default_provider="echo")))


# --- endpoint behavior -------------------------------------------------------


def test_chat_idempotency_key_replays_first_response() -> None:
    client = _client()
    body = {"model": "echo", "messages": [{"role": "user", "content": "hello"}]}
    headers = {"Idempotency-Key": "abc-123"}

    first = client.post("/v1/chat", json=body, headers=headers)
    assert first.status_code == 200
    assert first.headers.get("Idempotent-Replay") is None

    # Same key AND same body -> the stored first response is replayed.
    second = client.post("/v1/chat", json=body, headers=headers)
    assert second.status_code == 200
    assert second.headers["Idempotent-Replay"] == "true"
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["content"] == first.json()["content"]


def test_chat_idempotency_key_reused_with_different_body_is_422() -> None:
    client = _client()
    headers = {"Idempotency-Key": "reuse-1"}
    client.post(
        "/v1/chat",
        json={"model": "echo", "messages": [{"role": "user", "content": "hello"}]},
        headers=headers,
    )
    # Reusing the key for a *different* request body is a client error, not a
    # silent replay of the first (unrelated) response.
    resp = client.post(
        "/v1/chat",
        json={"model": "echo", "messages": [{"role": "user", "content": "totally different"}]},
        headers=headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "idempotency_error"


def test_chat_without_key_is_not_deduped() -> None:
    client = _client()
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}], "cache": False}
    r1 = client.post("/v1/chat", json=body)
    r2 = client.post("/v1/chat", json=body)
    # No idempotency key and caching off -> fresh ids each time.
    assert r1.json()["id"] != r2.json()["id"]


def test_different_keys_process_independently() -> None:
    client = _client()
    # cache:false isolates idempotency from the content cache.
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}], "cache": False}
    a = client.post("/v1/chat", json=body, headers={"Idempotency-Key": "k1"})
    b = client.post("/v1/chat", json=body, headers={"Idempotency-Key": "k2"})
    assert b.headers.get("Idempotent-Replay") is None
    assert a.json()["id"] != b.json()["id"]


def test_embeddings_idempotency_key_replays() -> None:
    client = _client()
    headers = {"Idempotency-Key": "emb-1"}
    body = {"model": "echo", "input": "x"}
    first = client.post("/v1/embeddings", json=body, headers=headers)
    second = client.post("/v1/embeddings", json=body, headers=headers)
    assert second.headers["Idempotent-Replay"] == "true"
    assert second.json()["embeddings"] == first.json()["embeddings"]


def test_embeddings_idempotency_key_reused_with_different_body_is_422() -> None:
    client = _client()
    headers = {"Idempotency-Key": "emb-reuse"}
    client.post("/v1/embeddings", json={"model": "echo", "input": "x"}, headers=headers)
    resp = client.post(
        "/v1/embeddings", json={"model": "echo", "input": "different"}, headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "idempotency_error"


def test_moderations_idempotency_key_replays() -> None:
    client = _client()
    headers = {"Idempotency-Key": "mod-1"}
    # omni-moderation-* routes to openai by name, so echo must be explicit.
    body = {"input": "x", "provider": "echo"}
    first = client.post("/v1/moderations", json=body, headers=headers)
    second = client.post("/v1/moderations", json=body, headers=headers)
    assert second.headers["Idempotent-Replay"] == "true"
    assert second.json() == first.json()


def test_moderations_idempotency_key_reused_with_different_body_is_422() -> None:
    client = _client()
    headers = {"Idempotency-Key": "mod-reuse"}
    client.post("/v1/moderations", json={"input": "x", "provider": "echo"}, headers=headers)
    resp = client.post(
        "/v1/moderations", json={"input": "different", "provider": "echo"}, headers=headers
    )
    assert resp.status_code == 422
    # /v1/moderations speaks the OpenAI error envelope, like /v1/chat/completions.
    assert resp.json()["error"]["type"] == "invalid_request_error"


# --- module lifecycle (claim / complete / release) ---------------------------


async def test_claim_then_complete_then_replay() -> None:
    cache = MemoryCache()
    fp = idempotency.fingerprint('{"m":"echo"}')
    first = await idempotency.claim(cache, "c1", "key", fp, ttl=60)
    assert first.kind == "proceed"  # we hold the sentinel

    await idempotency.complete(cache, "c1", "key", fp, {"id": "r1", "content": "ok"}, ttl=60)
    replay = await idempotency.claim(cache, "c1", "key", fp, ttl=60)
    assert replay.kind == "replay"
    assert replay.response == {"id": "r1", "content": "ok"}


async def test_claim_while_in_progress_is_conflict() -> None:
    cache = MemoryCache()
    fp = idempotency.fingerprint('{"m":"echo"}')
    assert (await idempotency.claim(cache, "c1", "key", fp, ttl=60)).kind == "proceed"
    # A second claim before complete() sees the in-progress sentinel.
    assert (await idempotency.claim(cache, "c1", "key", fp, ttl=60)).kind == "conflict"


async def test_claim_with_different_fingerprint_is_mismatch() -> None:
    cache = MemoryCache()
    await idempotency.claim(cache, "c1", "key", idempotency.fingerprint("body-a"), ttl=60)
    outcome = await idempotency.claim(cache, "c1", "key", idempotency.fingerprint("body-b"), ttl=60)
    assert outcome.kind == "mismatch"


async def test_release_frees_the_sentinel_for_retry() -> None:
    cache = MemoryCache()
    fp = idempotency.fingerprint("body")
    assert (await idempotency.claim(cache, "c1", "key", fp, ttl=60)).kind == "proceed"
    # Simulate the request erroring: release the sentinel...
    await idempotency.release(cache, "c1", "key")
    # ...so a retry can claim the key again instead of getting a 409.
    assert (await idempotency.claim(cache, "c1", "key", fp, ttl=60)).kind == "proceed"


async def test_null_cache_disables_idempotency() -> None:
    from rekai.cache import NullCache

    cache = NullCache()
    fp = idempotency.fingerprint("body")
    # Every claim "proceeds" (nothing is stored) — idempotency is a no-op.
    assert (await idempotency.claim(cache, "c1", "key", fp, ttl=60)).kind == "proceed"
    await idempotency.complete(cache, "c1", "key", fp, {"id": "x"}, ttl=60)
    assert (await idempotency.claim(cache, "c1", "key", fp, ttl=60)).kind == "proceed"


# --- per-client scoping ------------------------------------------------------
# Idempotency keys are caller-chosen and collide constantly ("req-1"), so the
# store namespaces them per client. Without that, one tenant's key would read
# another tenant's stored response, or claim the sentinel first and 409 them out
# of their own key.


async def test_same_key_and_body_from_two_clients_do_not_share_a_record() -> None:
    cache = MemoryCache()
    fp = idempotency.fingerprint('{"m":"echo"}')
    await idempotency.claim(cache, "key:aaa", "req-1", fp, ttl=60)
    await idempotency.complete(cache, "key:aaa", "req-1", fp, {"id": "tenant-a"}, ttl=60)

    # Tenant B uses the same key and the same body: it gets its own record, not
    # a replay of A's response, and is not blocked by A's completed one.
    outcome = await idempotency.claim(cache, "key:bbb", "req-1", fp, ttl=60)
    assert outcome.kind == "proceed"
    assert outcome.response is None

    # A's record is untouched and still replays for A.
    assert (await idempotency.claim(cache, "key:aaa", "req-1", fp, ttl=60)).response == {
        "id": "tenant-a"
    }


async def test_one_clients_in_flight_key_does_not_conflict_another() -> None:
    cache = MemoryCache()
    fp = idempotency.fingerprint("body")
    assert (await idempotency.claim(cache, "key:aaa", "req-1", fp, ttl=60)).kind == "proceed"
    # B is not held off by A's in-progress sentinel.
    assert (await idempotency.claim(cache, "key:bbb", "req-1", fp, ttl=60)).kind == "proceed"


def test_store_key_is_unambiguous_across_client_and_key_splits() -> None:
    # Naive concatenation would collide these two: "ab" + "c" == "a" + "bc".
    assert idempotency._store_key("ab", "c") != idempotency._store_key("a", "bc")


def test_endpoint_scopes_records_to_the_authenticated_key() -> None:
    app = create_app(
        Settings(environment="test", default_provider="echo", api_keys="sk-tenant-a,sk-tenant-b")
    )
    client = TestClient(app)
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}], "cache": False}
    headers = {"Idempotency-Key": "req-1"}

    a = client.post(
        "/v1/chat", json=body, headers={**headers, "Authorization": "Bearer sk-tenant-a"}
    )
    b = client.post(
        "/v1/chat", json=body, headers={**headers, "Authorization": "Bearer sk-tenant-b"}
    )
    assert a.status_code == 200 and b.status_code == 200
    # B must not be served A's stored response, nor 409'd by A's key.
    assert b.headers.get("Idempotent-Replay") is None
    assert b.json()["id"] != a.json()["id"]
    # A's own replay still works.
    again = client.post(
        "/v1/chat", json=body, headers={**headers, "Authorization": "Bearer sk-tenant-a"}
    )
    assert again.headers["Idempotent-Replay"] == "true"
    assert again.json()["id"] == a.json()["id"]


async def test_memory_cache_add_is_atomic_claim() -> None:
    cache = MemoryCache()
    assert await cache.add("k", "first", ttl=60) is True
    assert await cache.add("k", "second", ttl=60) is False  # already claimed
    await cache.delete("k")
    assert await cache.add("k", "third", ttl=60) is True  # freed


# --- wire-level conflict/release semantics -------------------------------------
# The claim lifecycle is unit-tested above; these cover what a *client* sees —
# a 409 while a twin request is in flight, and the sentinel being freed when the
# upstream call fails so an immediate retry re-runs instead of deadlocking.


async def test_chat_in_flight_key_returns_409(monkeypatch) -> None:
    backend = MemoryCache()
    monkeypatch.setattr("rekai.main.build_cache", lambda settings: backend)
    client = TestClient(
        create_app(Settings(environment="test", default_provider="echo", api_keys="sk-tenant-a"))
    )
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
    # Simulate a concurrent request already holding the sentinel for this key.
    fp = idempotency.fingerprint(ChatRequest.model_validate(body).model_dump_json())
    cid = auth.client_id("sk-tenant-a")
    assert (await idempotency.claim(backend, cid, "key-1", fp, ttl=60)).kind == "proceed"
    resp = client.post(
        "/v1/chat",
        json=body,
        headers={"Idempotency-Key": "key-1", "Authorization": "Bearer sk-tenant-a"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "idempotency_error"


async def test_completions_in_flight_key_returns_409(monkeypatch) -> None:
    backend = MemoryCache()
    monkeypatch.setattr("rekai.main.build_cache", lambda settings: backend)
    client = TestClient(
        create_app(Settings(environment="test", default_provider="echo", api_keys="sk-tenant-a"))
    )
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
    # The fingerprint on this route is computed on the *internal* request after
    # the OpenAI->ChatRequest mapping, not on the wire body.
    fp = idempotency.fingerprint(
        to_chat_request(ChatCompletionsRequest.model_validate(body)).model_dump_json()
    )
    cid = auth.client_id("sk-tenant-a")
    assert (await idempotency.claim(backend, cid, "key-1", fp, ttl=60)).kind == "proceed"
    resp = client.post(
        "/v1/chat/completions",
        json=body,
        headers={"Idempotency-Key": "key-1", "Authorization": "Bearer sk-tenant-a"},
    )
    assert resp.status_code == 409
    # The error leaves in the OpenAI envelope, not RekAI's own shape.
    assert "already being processed" in resp.json()["error"]["message"]


async def test_chat_key_released_when_provider_errors(monkeypatch) -> None:
    backend = MemoryCache()
    monkeypatch.setattr("rekai.main.build_cache", lambda settings: backend)
    client = TestClient(create_app(Settings(environment="test", default_provider="echo")))

    async def down(self, request, api_key):  # noqa: ANN001, ANN202
        raise ProviderError("upstream down", status_code=503)

    monkeypatch.setattr(EchoProvider, "chat", down)
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
    headers = {"Idempotency-Key": "key-1"}
    assert client.post("/v1/chat", json=body, headers=headers).status_code == 503
    monkeypatch.undo()  # restore echo (the app still uses `backend`)
    second = client.post("/v1/chat", json=body, headers=headers)
    assert second.status_code == 200  # would be a 409 had the sentinel not been released


async def test_embeddings_in_flight_key_returns_409(monkeypatch) -> None:
    backend = MemoryCache()
    monkeypatch.setattr("rekai.main.build_cache", lambda settings: backend)
    client = TestClient(
        create_app(Settings(environment="test", default_provider="echo", api_keys="sk-tenant-a"))
    )
    body = {"model": "echo", "input": "hi"}
    fp = idempotency.fingerprint(EmbeddingsRequest.model_validate(body).model_dump_json())
    cid = auth.client_id("sk-tenant-a")
    assert (await idempotency.claim(backend, cid, "key-1", fp, ttl=60)).kind == "proceed"
    resp = client.post(
        "/v1/embeddings",
        json=body,
        headers={"Idempotency-Key": "key-1", "Authorization": "Bearer sk-tenant-a"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"] == "idempotency_error"


async def test_embeddings_key_released_when_provider_errors(monkeypatch) -> None:
    backend = MemoryCache()
    monkeypatch.setattr("rekai.main.build_cache", lambda settings: backend)
    client = TestClient(create_app(Settings(environment="test", default_provider="echo")))

    async def down(self, inputs, model, api_key, **kwargs):  # noqa: ANN001, ANN202, ANN003
        raise ProviderError("upstream down", status_code=503)

    monkeypatch.setattr(EchoProvider, "embed", down)
    body = {"model": "echo", "input": "hi"}
    headers = {"Idempotency-Key": "key-1"}
    assert client.post("/v1/embeddings", json=body, headers=headers).status_code == 503
    monkeypatch.undo()
    assert client.post("/v1/embeddings", json=body, headers=headers).status_code == 200


# --- fail-open observability --------------------------------------------------
# Every other backend (cache, rate limiter, metrics store, keystore) logs when
# it degrades; the idempotency store's fail-open is where a silent failure costs
# real money (a replayed request re-processes) or turns visible (a stuck
# sentinel 409s retries until TTL). Each path must warn, not just swallow.


class _BoomCache:
    """Cache backend that raises on every operation (Redis outage stand-in)."""

    async def get(self, key):  # noqa: ANN001, ANN202
        raise ConnectionError("redis down")

    async def set(self, key, value, ttl):  # noqa: ANN001, ANN202
        raise ConnectionError("redis down")

    async def add(self, key, value, ttl):  # noqa: ANN001, ANN202
        raise ConnectionError("redis down")

    async def delete(self, key):  # noqa: ANN001, ANN202
        raise ConnectionError("redis down")

    @property
    def label(self) -> str:
        return "boom"


async def test_claim_fails_open_and_warns(caplog) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="rekai.idempotency"):
        outcome = await idempotency.claim(_BoomCache(), "c1", "key", "fp", ttl=60)
    assert outcome.kind == "proceed"  # fail-open preserved
    assert any("replay protection" in r.message for r in caplog.records)


async def test_complete_failure_warns(caplog) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="rekai.idempotency"):
        await idempotency.complete(_BoomCache(), "c1", "key", "fp", {"id": "x"}, ttl=60)
    assert any("re-process" in r.message for r in caplog.records)


async def test_release_failure_warns(caplog) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="rekai.idempotency"):
        await idempotency.release(_BoomCache(), "c1", "key")
    assert any("409" in r.message for r in caplog.records)


async def test_get_failure_treats_key_as_absent_and_warns(caplog) -> None:
    import logging

    class _HalfBoom(_BoomCache):
        async def add(self, key, value, ttl):  # noqa: ANN001, ANN202
            return False  # claim succeeds reading nothing: forces the _get path

    with caplog.at_level(logging.WARNING, logger="rekai.idempotency"):
        outcome = await idempotency.claim(_HalfBoom(), "c1", "key", "fp", ttl=60)
    assert outcome.kind == "proceed"
    assert any("treating the key as absent" in r.message for r in caplog.records)
