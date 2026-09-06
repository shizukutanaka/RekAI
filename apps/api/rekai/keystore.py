"""Dynamically-managed API keys, layered on top of the static REKAI_API_KEYS.

Static keys live in the environment and need a redeploy to change. This adds
a second, runtime-managed set of keys an operator can add/revoke through the
admin API (see ``/admin/keys`` in ``main.py``) without restarting the process.

Storage reuses whatever ``CacheBackend`` the deployment already has configured
(Redis when ``REKAI_REDIS_URL`` is set, else the process-local ``MemoryCache``)
instead of wiring up a dedicated store — one JSON blob under a single key,
mirroring the pattern in ``metrics_store.py``. With Redis this is shared across
workers/nodes; with the in-memory cache it's process-local only (same caveat as
the rate limiter and idempotency store without Redis).

Unlike BYOK (transient, never stored), dynamic keys *are* persisted server-side
— exactly the case ``rekai.security.KeyCipher`` exists for. Pass a ``cipher`` to
encrypt the blob at rest (e.g. in a shared Redis an operator doesn't fully
trust); omit it to store plaintext, same as before this existed.

``add``/``revoke`` are read-modify-write against that one blob, so they need to
serialize against each other. In the Redis-backed, multi-worker case — the
deployment this whole feature is *for* — two concurrent writes really can
interleave: ``cache.get``/``cache.set`` perform real network I/O and each
suspends the calling coroutine, letting another worker's request run in
between. Measured directly: two concurrent ``add()`` calls for different keys,
against a backend whose ``get``/``set`` actually await, left only one of the
two keys stored — the second writer's ``set`` silently overwrote the first's.
(The equivalent single-process, in-memory case happens not to race today,
because ``MemoryCache.get``/``set`` contain no real ``await`` and so never
yield control between them — but that is an accident of the in-memory
backend's implementation, not a guarantee, so the locking below applies
regardless of which backend is configured.) See ``_with_lock``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, TypeVar

from rekai.cache import CacheBackend
from rekai.logging_config import get_logger

if TYPE_CHECKING:
    from rekai.security import KeyCipher

logger = get_logger("rekai.keystore")

_CACHE_KEY = "rekai:api_keys:dynamic"
# Cache backends require a positive TTL; there's no "forever" option, so this
# stands in for one (renewed on every write, so it never lapses in practice).
_TTL_SECONDS = 10 * 365 * 24 * 3600

# A short-lived mutex around add()/revoke()'s critical section, keyed
# alongside the blob it protects. TTL is a safety net, not the expected hold
# time: a crashed holder's lock self-expires instead of wedging every future
# write, the same role a TTL plays for idempotency's in-progress sentinel.
_LOCK_KEY = _CACHE_KEY + ":lock"
_LOCK_TTL_SECONDS = 10
_LOCK_RETRY_DELAY_SECONDS = 0.05
_LOCK_MAX_ATTEMPTS = 40  # ~2s worst case under contention

T = TypeVar("T")


class DynamicKeyStore:
    def __init__(self, cache: CacheBackend, cipher: KeyCipher | None = None) -> None:
        self._cache = cache
        self._cipher = cipher

    async def list_keys(self) -> list[str]:
        raw = await self._cache.get(_CACHE_KEY)
        if not raw:
            return []
        if self._cipher is not None:
            try:
                raw = self._cipher.decrypt(raw)
            except ValueError:
                # Wrong/rotated encryption key, or a plaintext blob written
                # before encryption was turned on — treat as empty rather than
                # crash every request that checks auth. Warn loudly: this looks
                # identical to "all dynamic keys were revoked" from the caller's
                # side, and add()/revoke() calling list_keys() internally means
                # the next add() would silently overwrite the undecryptable
                # blob with a set containing only the new key.
                logger.warning(
                    "failed to decrypt dynamic key store — wrong or rotated "
                    "REKAI_DYNAMIC_KEYS_ENCRYPTION_KEY? Treating as empty; "
                    "existing dynamic keys are inaccessible until this is "
                    "fixed (they are not lost, but the next write will "
                    "overwrite them)."
                )
                return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [k for k in data if isinstance(k, str)] if isinstance(data, list) else []

    async def _save(self, keys: set[str]) -> None:
        payload = json.dumps(sorted(keys))
        if self._cipher is not None:
            payload = self._cipher.encrypt(payload)
        await self._cache.set(_CACHE_KEY, payload, ttl=_TTL_SECONDS)

    async def _with_lock(self, mutate: Callable[[], Awaitable[T]]) -> T:
        """Run ``mutate`` (a read-modify-write against the key set) holding a
        short-lived mutex, so a concurrent ``add``/``revoke`` can't observe the
        same read and silently overwrite this one's write (see module
        docstring). ``cache.add`` — Redis ``SET NX`` — is this codebase's
        atomic-claim idiom (``idempotency.py`` uses the same primitive for its
        in-progress sentinel); reused here as the lock itself.

        Fails open, in both senses, deliberately: a lock-backend error is
        treated as an acquired lock, and exhausting the retry budget under
        contention proceeds unlocked rather than failing the request. Either
        one *can* reproduce the very race this exists to prevent, but refusing
        to manage keys at all because the *lock* is unavailable is worse for an
        operator who is, for instance, trying to revoke a compromised key.
        """
        for _ in range(_LOCK_MAX_ATTEMPTS):
            try:
                acquired = await self._cache.add(_LOCK_KEY, "1", _LOCK_TTL_SECONDS)
            except Exception:  # pragma: no cover - fail open on backend error
                acquired = True
            if acquired:
                try:
                    return await mutate()
                finally:
                    try:
                        await self._cache.delete(_LOCK_KEY)
                    except Exception:  # pragma: no cover - fail open
                        pass
            await asyncio.sleep(_LOCK_RETRY_DELAY_SECONDS)
        return await mutate()

    async def add(self, key: str) -> None:
        async def _mutate() -> None:
            keys = set(await self.list_keys())
            keys.add(key)
            await self._save(keys)

        await self._with_lock(_mutate)

    async def revoke(self, key: str) -> bool:
        async def _mutate() -> bool:
            keys = set(await self.list_keys())
            if key not in keys:
                return False
            keys.discard(key)
            await self._save(keys)
            return True

        return await self._with_lock(_mutate)
