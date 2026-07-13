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
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from rekai.cache import CacheBackend
from rekai.logging_config import get_logger

if TYPE_CHECKING:
    from rekai.security import KeyCipher

logger = get_logger("rekai.keystore")

_CACHE_KEY = "rekai:api_keys:dynamic"
# Cache backends require a positive TTL; there's no "forever" option, so this
# stands in for one (renewed on every write, so it never lapses in practice).
_TTL_SECONDS = 10 * 365 * 24 * 3600


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

    async def add(self, key: str) -> None:
        keys = set(await self.list_keys())
        keys.add(key)
        await self._save(keys)

    async def revoke(self, key: str) -> bool:
        keys = set(await self.list_keys())
        if key not in keys:
            return False
        keys.discard(key)
        await self._save(keys)
        return True
