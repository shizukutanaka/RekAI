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
"""

from __future__ import annotations

import json

from rekai.cache import CacheBackend

_CACHE_KEY = "rekai:api_keys:dynamic"
# Cache backends require a positive TTL; there's no "forever" option, so this
# stands in for one (renewed on every write, so it never lapses in practice).
_TTL_SECONDS = 10 * 365 * 24 * 3600


class DynamicKeyStore:
    def __init__(self, cache: CacheBackend) -> None:
        self._cache = cache

    async def list_keys(self) -> list[str]:
        raw = await self._cache.get(_CACHE_KEY)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [k for k in data if isinstance(k, str)] if isinstance(data, list) else []

    async def add(self, key: str) -> None:
        keys = set(await self.list_keys())
        keys.add(key)
        await self._cache.set(_CACHE_KEY, json.dumps(sorted(keys)), ttl=_TTL_SECONDS)

    async def revoke(self, key: str) -> bool:
        keys = set(await self.list_keys())
        if key not in keys:
            return False
        keys.discard(key)
        await self._cache.set(_CACHE_KEY, json.dumps(sorted(keys)), ttl=_TTL_SECONDS)
        return True
