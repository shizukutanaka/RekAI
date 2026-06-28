import pytest

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


async def test_memory_cache_roundtrip() -> None:
    cache = MemoryCache()
    await cache.set("k", "v", ttl=10)
    assert await cache.get("k") == "v"


async def test_memory_cache_expiry() -> None:
    cache = MemoryCache()
    await cache.set("k", "v", ttl=0)
    assert await cache.get("k") is None


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
