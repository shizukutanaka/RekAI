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


@pytest.mark.parametrize(
    "label_cache,expected",
    [(MemoryCache(), "memory"), (NullCache(), "disabled")],
)
def test_labels(label_cache, expected) -> None:
    assert label_cache.label == expected
