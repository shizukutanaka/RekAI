"""Idempotency-Key support.

A client can send an ``Idempotency-Key`` header (a unique id, e.g. a UUID) with a
write request. If it retries the same operation with the same key, RekAI returns
the **stored response** from the first call instead of processing it again — so a
network blip or an automatic retry can't double-charge or duplicate work.

Keys are stored in the response cache backend (Redis/memory) under a separate
namespace; when caching is disabled (NullCache) idempotency is a no-op.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from rekai.cache import CacheBackend

_PREFIX = "rekai:idem:"


def _store_key(raw_key: str) -> str:
    return _PREFIX + hashlib.sha256(raw_key.encode()).hexdigest()


async def get(cache: CacheBackend, raw_key: str) -> dict[str, Any] | None:
    """Return the stored response payload for ``raw_key``, or None."""
    stored = await cache.get(_store_key(raw_key))
    if stored is None:
        return None
    try:
        return json.loads(stored)
    except json.JSONDecodeError:  # pragma: no cover - defensive
        return None


async def store(cache: CacheBackend, raw_key: str, response_json: str, ttl: int) -> None:
    """Persist a response payload (already serialized) under ``raw_key``."""
    await cache.set(_store_key(raw_key), response_json, ttl)
