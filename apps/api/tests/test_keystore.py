"""Unit tests for the runtime-managed API key store."""

from __future__ import annotations

from rekai.cache import MemoryCache, NullCache
from rekai.keystore import DynamicKeyStore


async def test_list_keys_empty_by_default() -> None:
    store = DynamicKeyStore(MemoryCache())
    assert await store.list_keys() == []


async def test_add_then_list() -> None:
    store = DynamicKeyStore(MemoryCache())
    await store.add("sk-dyn-a")
    await store.add("sk-dyn-b")
    assert sorted(await store.list_keys()) == ["sk-dyn-a", "sk-dyn-b"]


async def test_add_is_idempotent() -> None:
    store = DynamicKeyStore(MemoryCache())
    await store.add("sk-dyn-a")
    await store.add("sk-dyn-a")
    assert await store.list_keys() == ["sk-dyn-a"]


async def test_revoke_removes_a_key_and_reports_success() -> None:
    store = DynamicKeyStore(MemoryCache())
    await store.add("sk-dyn-a")
    await store.add("sk-dyn-b")
    assert await store.revoke("sk-dyn-a") is True
    assert await store.list_keys() == ["sk-dyn-b"]


async def test_revoke_unknown_key_is_a_noop() -> None:
    store = DynamicKeyStore(MemoryCache())
    await store.add("sk-dyn-a")
    assert await store.revoke("sk-never-added") is False
    assert await store.list_keys() == ["sk-dyn-a"]


async def test_null_cache_backend_never_persists() -> None:
    # If an operator disables caching entirely, the store degrades to a no-op
    # instead of raising — same fallback behaviour as everything else backed
    # by CacheBackend (documented as a misconfiguration, not a crash).
    store = DynamicKeyStore(NullCache())
    await store.add("sk-dyn-a")
    assert await store.list_keys() == []
