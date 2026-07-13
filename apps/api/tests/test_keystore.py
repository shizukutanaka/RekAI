"""Unit tests for the runtime-managed API key store."""

from __future__ import annotations

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
