"""Unit tests for the runtime-managed API key store."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi.testclient import TestClient

from rekai.auth import client_id as _client_id
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


async def test_key_with_future_expiry_is_listed_and_expired_one_is_not() -> None:
    store = DynamicKeyStore(MemoryCache())
    await store.add("sk-live", expires_at=time.time() + 3600)
    await store.add("sk-dead", expires_at=time.time() - 1)
    await store.add("sk-forever")
    assert sorted(await store.list_keys()) == ["sk-forever", "sk-live"]


async def test_key_expiries_reports_only_unexpired_keys() -> None:
    store = DynamicKeyStore(MemoryCache())
    live_at = time.time() + 60
    await store.add("sk-live", expires_at=live_at)
    await store.add("sk-dead", expires_at=time.time() - 1)
    await store.add("sk-forever")
    expiries = await store.key_expiries()
    assert expiries == {"sk-live": live_at, "sk-forever": None}


async def test_legacy_list_blob_migrates_to_no_expiry() -> None:
    """A blob written by a pre-expiry RekAI (a bare JSON key list) must keep
    working — operators upgrade the binary before touching their key data."""
    cache = MemoryCache()
    await cache.set("rekai:api_keys:dynamic", json.dumps(["sk-old"]), ttl=3600)
    store = DynamicKeyStore(cache)
    assert await store.list_keys() == ["sk-old"]
    # And a subsequent write upgrades the blob in place.
    await store.add("sk-new", expires_at=time.time() + 10)
    expiries = await store.key_expiries()
    assert set(expiries) == {"sk-old", "sk-new"}
    assert expiries["sk-old"] is None


async def test_revoke_still_finds_an_expired_key() -> None:
    store = DynamicKeyStore(MemoryCache())
    await store.add("sk-dead", expires_at=time.time() - 1)
    assert await store.revoke("sk-dead") is True
    assert await store.list_keys() == []


def test_dynamic_keys_without_cache_logs_a_warning(capsys) -> None:
    # With NullCache the store can never persist, so added keys would silently
    # vanish between requests — the gateway says so loudly at startup.
    from rekai.config import Settings
    from rekai.main import create_app

    create_app(
        Settings(
            environment="test",
            default_provider="echo",
            dynamic_keys_enabled=True,
            cache_enabled=False,
        )
    )
    assert "REKAI_DYNAMIC_KEYS_ENABLED" in capsys.readouterr().out


async def test_expired_keys_are_pruned_on_the_next_write() -> None:
    # _load keeps expired entries (so revoke can report "found"), but the blob
    # must not accumulate them forever: every write path prunes them.
    cache = MemoryCache()
    store = DynamicKeyStore(cache)
    await store.add("sk-live", expires_at=None)
    await store.add("sk-dead", expires_at=time.time() - 1)
    await store.add("sk-new", expires_at=None)
    stored = json.loads(await cache.get("rekai:api_keys:dynamic"))
    assert sorted(stored) == ["sk-live", "sk-new"]


async def test_add_is_refused_when_the_store_is_full(monkeypatch) -> None:
    import rekai.keystore as keystore

    monkeypatch.setattr(keystore, "_MAX_DYNAMIC_KEYS", 2)
    store = DynamicKeyStore(MemoryCache())
    assert await store.add("sk-a") is True
    assert await store.add("sk-b") is True
    assert await store.add("sk-c") is False
    # Nothing was written for the refused key.
    assert sorted(await store.list_keys()) == ["sk-a", "sk-b"]
    # Re-adding an existing key refreshes it — it consumes no new slot.
    assert await store.add("sk-a") is True


async def test_expired_keys_do_not_count_toward_the_cap(monkeypatch) -> None:
    import rekai.keystore as keystore

    monkeypatch.setattr(keystore, "_MAX_DYNAMIC_KEYS", 1)
    store = DynamicKeyStore(MemoryCache())
    await store.add("sk-dead", expires_at=time.time() - 1)
    # The one slot is occupied by a dead key only, so the add still fits.
    assert await store.add("sk-live") is True


def test_admin_add_key_returns_409_when_store_full(monkeypatch) -> None:
    import rekai.keystore as keystore
    from rekai.config import Settings
    from rekai.main import create_app

    monkeypatch.setattr(keystore, "_MAX_DYNAMIC_KEYS", 1)
    settings = Settings(
        environment="test",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        dynamic_keys_enabled=True,
    )
    client = TestClient(create_app(settings))
    headers = {"Authorization": "Bearer sk-admin-1"}
    assert client.post("/admin/keys", json={"key": "sk-first"}, headers=headers).status_code == 201
    resp = client.post("/admin/keys", json={"key": "sk-second"}, headers=headers)
    assert resp.status_code == 409
    assert resp.json()["error"] == "key_store_full"


async def test_credit_grant_spend_and_balance() -> None:
    store = DynamicKeyStore(MemoryCache())
    assert await store.credit_balance("key:abc") is None  # no entry -> unbilled

    assert await store.grant_credit("key:abc", 5.0) is True
    assert await store.credit_balance("key:abc") == 5.0

    # Granting again tops the entry up rather than replacing it.
    assert await store.grant_credit("key:abc", 2.5) is True
    assert await store.credit_balance("key:abc") == 7.5

    remaining = await store.spend_credit("key:abc", 7.0)
    assert remaining == 0.5

    # The balance may go negative: the in-flight request is still billed and
    # the middleware rejects the *next* one.
    assert await store.spend_credit("key:abc", 1.0) == -0.5
    assert await store.credit_balance("key:abc") == -0.5


async def test_spend_credit_is_a_noop_without_an_entry() -> None:
    store = DynamicKeyStore(MemoryCache())
    assert await store.spend_credit("key:abc", 1.0) is None
    # A free/unpriced request never debits either.
    await store.grant_credit("key:abc", 1.0)
    assert await store.spend_credit("key:abc", 0.0) is None
    assert await store.spend_credit("key:abc", None) is None
    assert await store.credit_balance("key:abc") == 1.0


async def test_revoke_drops_the_credit_entry() -> None:
    store = DynamicKeyStore(MemoryCache())
    await store.add("sk-dyn-1")
    await store.grant_credit(_client_id("sk-dyn-1"), 3.0)
    assert await store.revoke("sk-dyn-1") is True
    # A re-added key must not inherit the predecessor's balance.
    assert await store.credit_balance(_client_id("sk-dyn-1")) is None


async def test_encrypted_store_roundtrips_credits() -> None:
    cache = MemoryCache()
    cipher = KeyCipher(generate_key())
    store = DynamicKeyStore(cache, cipher)
    await store.grant_credit("key:abc", 4.0)
    assert await store.credit_balance("key:abc") == 4.0
    assert "key:abc" not in (await cache.get("rekai:api_keys:credits") or "")


async def test_credit_cap_refuses_new_entries(monkeypatch) -> None:
    import rekai.keystore as keystore

    monkeypatch.setattr(keystore, "_MAX_CREDIT_ENTRIES", 1)
    store = DynamicKeyStore(MemoryCache())
    assert await store.grant_credit("key:a", 1.0) is True
    assert await store.grant_credit("key:b", 1.0) is False
    # ...while topping up an existing entry still fits.
    assert await store.grant_credit("key:a", 1.0) is True
    assert await store.credit_balance("key:a") == 2.0


def test_admin_grant_credit_and_402_gate() -> None:
    """Sellable prepaid access: grant credits, spend them on billed calls,
    and get a 402 once the balance is gone."""
    from rekai.config import Settings
    from rekai.main import create_app
    from rekai.providers import register_provider
    from rekai.providers.base import Provider, ProviderResult
    from rekai.schemas import Usage

    class PricedProvider(Provider):
        name = "priced"
        requires_key = False

        async def chat(self, request, api_key) -> ProviderResult:
            return ProviderResult(
                model=request.model or "m",
                content="ok",
                usage=Usage(prompt_tokens=1_000_000, completion_tokens=0, total_tokens=1_000_000),
            )

    register_provider(PricedProvider())
    settings = Settings(
        environment="test",
        default_provider="priced",
        api_keys="sk-buyer,sk-free",
        dynamic_keys_enabled=True,
        admin_key="sk-admin-1",
        rate_limit_enabled=False,
        pricing_overrides="m:10.0:10.0",  # $10 per 1M tokens -> $10/request
    )
    client = TestClient(create_app(settings))
    admin = {"Authorization": "Bearer sk-admin-1"}
    body = {"messages": [{"role": "user", "content": "hi"}], "model": "m"}

    # Top up $15 on the buyer's key — the revenue-bearing primitive.
    resp = client.post("/admin/keys/sk-buyer/credits", json={"usd_amount": 15.0}, headers=admin)
    assert resp.status_code == 200
    assert resp.json()["status"] == "granted"
    assert resp.json()["credit_remaining_usd"] == 15.0

    # One $10 request succeeds and debits the balance to $5.
    ok = client.post("/v1/chat", json=body, headers={"Authorization": "Bearer sk-buyer"})
    assert ok.status_code == 200
    # Second request: balance is now $5, still positive -> allowed, then $-5.
    ok2 = client.post("/v1/chat", json=body, headers={"Authorization": "Bearer sk-buyer"})
    assert ok2.status_code == 200
    # Third: balance exhausted -> prepaid gate rejects before any work.
    dead = client.post("/v1/chat", json=body, headers={"Authorization": "Bearer sk-buyer"})
    assert dead.status_code == 402
    assert dead.json()["error"] == "credit_exhausted"

    # A key that was never granted credit is unbilled and unaffected.
    free = client.post("/v1/chat", json=body, headers={"Authorization": "Bearer sk-free"})
    assert free.status_code == 200


def test_admin_mint_with_credit_usd_lists_balance() -> None:
    from rekai.config import Settings
    from rekai.main import create_app

    settings = Settings(
        environment="test",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        dynamic_keys_enabled=True,
    )
    client = TestClient(create_app(settings))
    headers = {"Authorization": "Bearer sk-admin-1"}
    resp = client.post(
        "/admin/keys",
        json={"key": "sk-dyn-paid", "credit_usd": 25.0},
        headers=headers,
    )
    assert resp.status_code == 201

    listed = client.get("/admin/keys", headers=headers).json()
    assert listed["credit_remaining_usd"] == {"sk-d…paid": 25.0}


async def test_expired_key_pruned_on_write_forfeits_credit() -> None:
    """A key dropped by the add() expiry prune loses its balance too — a
    re-added key must not inherit the predecessor's credit."""
    store = DynamicKeyStore(MemoryCache())
    await store.add("sk-dyn-old", expires_at=time.time() - 1)
    await store.grant_credit(_client_id("sk-dyn-old"), 9.0)
    # The next write prunes the dead key and its balance.
    await store.add("sk-dyn-new")
    assert await store.credit_balance(_client_id("sk-dyn-old")) is None


async def test_sub_quantum_grant_stays_spendable() -> None:
    """A positive top-up must land above zero — rounding to the ledger's
    6-dp quantum must not mint an already-exhausted balance."""
    store = DynamicKeyStore(MemoryCache())
    assert await store.grant_credit("key:abc", 0.0000004) is True
    assert (await store.credit_balance("key:abc")) > 0


def test_admin_listing_shows_static_key_balances() -> None:
    from rekai.config import Settings
    from rekai.main import create_app

    settings = Settings(
        environment="test",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        dynamic_keys_enabled=True,
        api_keys="sk-static-paid",
    )
    client = TestClient(create_app(settings))
    headers = {"Authorization": "Bearer sk-admin-1"}
    resp = client.post(
        "/admin/keys/sk-static-paid/credits",
        json={"usd_amount": 7.0},
        headers=headers,
    )
    assert resp.status_code == 200
    listed = client.get("/admin/keys", headers=headers).json()
    assert listed["credit_remaining_usd"] == {"sk-s…paid": 7.0}
