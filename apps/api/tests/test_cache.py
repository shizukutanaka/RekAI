import pytest

from rekai import cache as cache_module
from rekai.cache import MemoryCache, NullCache, cache_key
from rekai.schemas import ChatMessage, ChatRequest


def _req(content: str = "hi", **kwargs) -> ChatRequest:
    kwargs.setdefault("model", "echo")
    return ChatRequest(messages=[ChatMessage(role="user", content=content)], **kwargs)


def test_cache_key_is_deterministic() -> None:
    assert cache_key(_req(), "echo") == cache_key(_req(), "echo")


def test_cache_key_changes_with_content() -> None:
    assert cache_key(_req("a"), "echo") != cache_key(_req("b"), "echo")


def test_cache_key_changes_with_provider() -> None:
    assert cache_key(_req(), "echo") != cache_key(_req(), "openai")


def test_cache_key_changes_with_tools() -> None:
    # Same messages but different tools must NOT collide (else the cached
    # tool-less reply would be served for a tools request).
    tools = [{"type": "function", "function": {"name": "get_weather"}}]
    assert cache_key(_req(), "echo") != cache_key(_req(tools=tools), "echo")


def test_cache_key_changes_with_tool_choice() -> None:
    tools = [{"type": "function", "function": {"name": "get_weather"}}]
    a = cache_key(_req(tools=tools, tool_choice="auto"), "echo")
    b = cache_key(_req(tools=tools, tool_choice="required"), "echo")
    assert a != b


async def test_memory_cache_roundtrip() -> None:
    cache = MemoryCache()
    await cache.set("k", "v", ttl=10)
    assert await cache.get("k") == "v"


async def test_memory_cache_expiry() -> None:
    cache = MemoryCache()
    await cache.set("k", "v", ttl=0)
    assert await cache.get("k") is None


async def test_memory_cache_evicts_expired_at_capacity() -> None:
    cache = MemoryCache(max_entries=1)
    await cache.set("old", "v", ttl=0)  # already expired
    await cache.set("a", "v", ttl=10)  # at capacity -> prunes "old", then stores
    assert "old" not in cache._store
    assert await cache.get("a") == "v"


async def test_memory_cache_add_reclaims_an_expired_key(monkeypatch) -> None:
    # ``add`` must use the same expiry boundary as ``get``: an entry whose
    # expires_at has arrived is dead, so the key is claimable again. A ttl=0
    # sentinel used to be treated as live by ``add`` (``>= now``) while ``get``
    # reported it gone (``<= now``), wedging idempotency claims on that key.
    # The clock is frozen so the boundary (expires_at == now) is hit exactly —
    # on a coarse-resolution clock it otherwise reproduces only intermittently.
    monkeypatch.setattr(cache_module.time, "time", lambda: 1_000.0)
    cache = MemoryCache()
    await cache.set("k", "old", ttl=0)
    # Note: no ``get`` first — a read would evict the dead entry itself and
    # hide the bug. ``add`` must judge the expiry on its own.
    assert await cache.add("k", "new", ttl=60) is True
    assert await cache.get("k") == "new"


async def test_memory_cache_add_refuses_a_live_key() -> None:
    cache = MemoryCache()
    assert await cache.add("k", "first", ttl=60) is True
    assert await cache.add("k", "second", ttl=60) is False
    assert await cache.get("k") == "first"


async def test_null_cache_always_misses() -> None:
    cache = NullCache()
    await cache.set("k", "v", ttl=10)
    assert await cache.get("k") is None


class _BrokenRedis:
    """A stand-in for a configured-but-unreachable Redis client."""

    async def get(self, key):
        raise RuntimeError("redis is down")

    async def set(self, *a, **k):
        raise RuntimeError("redis is down")

    async def delete(self, key):
        raise RuntimeError("redis is down")


async def test_redis_cache_fails_open_on_errors() -> None:
    # A configured Redis that is unreachable must NOT turn requests into 500s.
    # get() returns a miss, set()/add()/delete() become no-ops, and the backend
    # transparently downgrades to an in-process cache afterwards. (Reproduces the
    # bug where an unreachable REKAI_REDIS_URL raised ConnectionError on every
    # /v1/chat and returned Internal Server Error.)
    import redis.asyncio as redis  # noqa: F401  (real module must be importable)

    cache = cache_module.RedisCache.__new__(cache_module.RedisCache)
    cache._client = _BrokenRedis()  # bypass the real from_url() connection
    cache._local = None
    cache._degraded = False

    # get swallows the error and returns a miss; subsequent writes are no-ops.
    assert await cache.get("k") is None
    await cache.set("k", "v", ttl=10)
    await cache.delete("k")
    # set()/delete() are no-ops against the broken backend, so a get is still a miss.
    assert await cache.get("k") is None
    # And we have silently fallen back to an in-process cache for the rest of
    # the process, so a later set+get works locally even though Redis is dead.
    assert cache._local is not None
    await cache.set("k", "v", ttl=10)
    assert await cache.get("k") == "v"
    # label still reports the configured backend.
    assert cache.label == "redis"


@pytest.mark.parametrize(
    "label_cache,expected",
    [(MemoryCache(), "memory"), (NullCache(), "disabled")],
)
def test_labels(label_cache, expected) -> None:
    assert label_cache.label == expected


async def test_redis_cache_add_fails_open_by_claiming_locally() -> None:
    # `add` is the codebase's atomic-claim idiom — the idempotency in-progress
    # sentinel is a `cache.add`. Every other RedisCache method fails open by
    # reporting an *absence*: a miss, a no-op write. `add` used to fail open by
    # returning False, which reports a *fact* — "someone else holds this key" —
    # when the truth is that we could not look. A caller using it as a lock
    # would deny service on a Redis blip.
    cache = cache_module.RedisCache.__new__(cache_module.RedisCache)
    cache._client = _BrokenRedis()
    cache._local = None
    cache._degraded = False

    # The claim succeeds against the fallback that the degrade installs...
    assert await cache.add("claim", "sentinel", ttl=10) is True
    # ...and the key is genuinely held afterwards, so a second claim is refused.
    assert await cache.add("claim", "sentinel", ttl=10) is False
    # Both answers are true statements about the fallback, which is the point.
    assert await cache.get("claim") == "sentinel"


async def test_idempotency_claim_proceeds_when_redis_is_down() -> None:
    # The behaviour that matters to a caller: a Redis outage must not make the
    # gateway believe a request is already in flight. Verified end to end through
    # the idempotency layer rather than only at the cache boundary.
    from rekai import idempotency

    cache = cache_module.RedisCache.__new__(cache_module.RedisCache)
    cache._client = _BrokenRedis()
    cache._local = None
    cache._degraded = False

    first = await idempotency.claim(cache, "client-a", "key-1", "fingerprint", 60)

    assert first.kind == "proceed"
