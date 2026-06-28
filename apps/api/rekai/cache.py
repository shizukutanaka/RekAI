"""Response cache.

Uses Redis when ``REKAI_REDIS_URL`` is configured, otherwise falls back to a
process-local in-memory cache so the stack works with zero external services.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Protocol

from rekai.config import Settings
from rekai.schemas import ChatRequest


def cache_key(request: ChatRequest, provider: str) -> str:
    """A deterministic key for a (provider, model, messages, temperature) tuple."""
    payload = {
        "provider": provider,
        "model": request.model,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "messages": [m.model_dump() for m in request.messages],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "rekai:chat:" + hashlib.sha256(raw.encode()).hexdigest()


class CacheBackend(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl: int) -> None: ...
    @property
    def label(self) -> str: ...


class MemoryCache:
    """A tiny TTL cache backed by a dict. Not shared across processes."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, str]] = {}

    async def get(self, key: str) -> str | None:
        item = self._store.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at < time.time():
            self._store.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: str, ttl: int) -> None:
        self._store[key] = (time.time() + ttl, value)

    @property
    def label(self) -> str:
        return "memory"


class RedisCache:
    def __init__(self, url: str) -> None:
        import redis.asyncio as redis  # imported lazily so redis is optional at runtime

        self._client = redis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> str | None:
        value = await self._client.get(key)
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    async def set(self, key: str, value: str, ttl: int) -> None:
        await self._client.set(key, value, ex=ttl)

    @property
    def label(self) -> str:
        return "redis"


class NullCache:
    """Disabled cache — always misses."""

    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ttl: int) -> None:
        return None

    @property
    def label(self) -> str:
        return "disabled"


def build_cache(settings: Settings) -> CacheBackend:
    if not settings.cache_enabled:
        return NullCache()
    if settings.redis_url:
        try:
            return RedisCache(settings.redis_url)
        except Exception:  # pragma: no cover - fall back if redis client init fails
            return MemoryCache()
    return MemoryCache()
