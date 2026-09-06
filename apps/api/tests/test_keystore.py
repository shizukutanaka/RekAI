"""Unit tests for the runtime-managed API key store."""

from __future__ import annotations

import asyncio
import logging

from rekai.cache import MemoryCache, NullCache
from rekai.keystore import DynamicKeyStore
from rekai.security import KeyCipher, generate_key


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


async def test_encrypted_store_roundtrips() -> None:
    cache = MemoryCache()
    cipher = KeyCipher(generate_key())
    store = DynamicKeyStore(cache, cipher)
    await store.add("sk-dyn-a")
    await store.add("sk-dyn-b")
    assert sorted(await store.list_keys()) == ["sk-dyn-a", "sk-dyn-b"]


async def test_encrypted_blob_is_not_plaintext_in_the_cache() -> None:
    cache = MemoryCache()
    cipher = KeyCipher(generate_key())
    store = DynamicKeyStore(cache, cipher)
    await store.add("sk-super-secret")
    raw = await cache.get("rekai:api_keys:dynamic")
    assert raw is not None
    assert "sk-super-secret" not in raw


async def test_wrong_decryption_key_degrades_to_empty_not_a_crash() -> None:
    cache = MemoryCache()
    await DynamicKeyStore(cache, KeyCipher(generate_key())).add("sk-dyn-a")
    reader = DynamicKeyStore(cache, KeyCipher(generate_key()))  # different key
    assert await reader.list_keys() == []


async def test_wrong_decryption_key_logs_a_warning(caplog) -> None:
    # The degrade-to-empty path looks identical to "all keys were revoked"
    # from the caller's side unless this is logged loudly.
    cache = MemoryCache()
    await DynamicKeyStore(cache, KeyCipher(generate_key())).add("sk-dyn-a")
    reader = DynamicKeyStore(cache, KeyCipher(generate_key()))
    with caplog.at_level(logging.WARNING, logger="rekai.keystore"):
        await reader.list_keys()
    assert any("decrypt" in r.message for r in caplog.records)


async def test_reading_a_plaintext_blob_with_a_cipher_degrades_to_empty() -> None:
    # Simulates turning encryption on after keys were already stored in
    # plaintext — the old blob doesn't decrypt, so it's treated as empty
    # rather than crashing every request that checks auth.
    cache = MemoryCache()
    await DynamicKeyStore(cache).add("sk-dyn-a")  # no cipher -> plaintext
    reader = DynamicKeyStore(cache, KeyCipher(generate_key()))
    assert await reader.list_keys() == []


class _AsyncIOCache:
    """Wraps another CacheBackend so get/set/add/delete actually suspend the
    calling coroutine (an ``asyncio.sleep(0)`` around each), the way a real
    Redis client's I/O does and ``MemoryCache``'s in-memory calls never do.

    ``MemoryCache.get``/``set`` contain no genuine ``await``, so two coroutines
    calling ``add()`` back-to-back never actually interleave against it —
    which would make a concurrency test here pass by accident and prove
    nothing about the Redis-backed deployment the locking exists for. This
    forces the real interleaving deterministically instead of depending on
    timing.
    """

    def __init__(self, inner) -> None:
        self._inner = inner

    async def get(self, key: str) -> str | None:
        await asyncio.sleep(0)
        return await self._inner.get(key)

    async def set(self, key: str, value: str, ttl: int) -> None:
        await asyncio.sleep(0)
        await self._inner.set(key, value, ttl)

    async def add(self, key: str, value: str, ttl: int) -> bool:
        await asyncio.sleep(0)
        return await self._inner.add(key, value, ttl)

    async def delete(self, key: str) -> None:
        await asyncio.sleep(0)
        await self._inner.delete(key)


async def test_concurrent_add_does_not_lose_a_key() -> None:
    """Two admins (or one retried request) adding different keys at the same
    time must both stick — not have one silently overwritten by the other's
    stale read. Reproduces the race from the module docstring: without the
    lock in add(), this leaves only one of the two keys stored."""
    store = DynamicKeyStore(_AsyncIOCache(MemoryCache()))
    await asyncio.gather(store.add("sk-dyn-a"), store.add("sk-dyn-b"))
    assert sorted(await store.list_keys()) == ["sk-dyn-a", "sk-dyn-b"]


async def test_concurrent_add_and_revoke_do_not_lose_each_other() -> None:
    store = DynamicKeyStore(_AsyncIOCache(MemoryCache()))
    await store.add("sk-existing")
    await asyncio.gather(store.add("sk-new"), store.revoke("sk-existing"))
    assert sorted(await store.list_keys()) == ["sk-new"]


async def test_many_concurrent_adds_all_survive() -> None:
    # More than a pair, to show the lock actually serializes N-way contention
    # rather than only happening to work for two. Kept small: each writer
    # beyond the one holding the lock waits out a retry poll, and this is an
    # admin-only, low-frequency operation, not a hot path worth over-testing.
    store = DynamicKeyStore(_AsyncIOCache(MemoryCache()))
    keys = [f"sk-dyn-{i}" for i in range(6)]
    await asyncio.gather(*(store.add(k) for k in keys))
    assert sorted(await store.list_keys()) == sorted(keys)
