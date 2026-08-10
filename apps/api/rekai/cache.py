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
    """A deterministic key for the request fields that affect the response.

    Includes ``tools``/``tool_choice`` — otherwise two requests with identical
    messages but different tools would collide and return the wrong response.
    """
    payload = {
        "provider": provider,
        "model": request.model,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "messages": [m.model_dump() for m in request.messages],
        "tools": request.tools,
        "tool_choice": request.tool_choice,
        # A JSON-mode request and a plain one must not share a cache entry.
        "response_format": request.response_format,
        # Prompt-cache breakpoints change what the provider is asked to do (and
        # the cost breakdown we report back), so they key separately. Per-message
        # cache_control already rides along in `messages` above.
        "cache_control": request.cache_control,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "rekai:chat:" + hashlib.sha256(raw.encode()).hexdigest()


def semantic_bucket(request: ChatRequest, provider: str, client_id: str) -> str:
    """The partition a semantic cache entry lives in.

    Deliberately ``cache_key``'s payload **minus ``messages``** — the message
    text is what the embedding compares, everything else must match exactly for
    a paraphrase hit to be valid. Keeping the two side by side is the point: the
    bucket used to be a bare
    ``f"{provider}:{model}:{temperature}:{max_tokens}"`` string, which reproduced
    the very collisions the comments above warn about (a ``response_format``
    JSON-mode request could be answered from a prose entry).

    ``client_id`` is in the bucket because a semantic hit answers a prompt the
    caller never sent — sharing across tenants would hand one an answer to
    another's question. The exact cache can share freely; a hit there requires
    the caller to have sent the identical prompt itself.
    """
    payload = {
        "client": client_id,
        "provider": provider,
        "model": request.model,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "tools": request.tools,
        "tool_choice": request.tool_choice,
        "response_format": request.response_format,
        "cache_control": request.cache_control,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def embedding_cache_key(provider: str, model: str, inputs: list[str]) -> str:
    """A deterministic key for an embeddings request."""
    payload = {"provider": provider, "model": model, "inputs": inputs}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "rekai:embed:" + hashlib.sha256(raw.encode()).hexdigest()


class CacheBackend(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl: int) -> None: ...
    async def add(self, key: str, value: str, ttl: int) -> bool:
        """Atomically set ``key`` only if absent. Returns True if it was set
        (the caller won the race), False if a live value already existed.
        Used by idempotency to claim an in-progress sentinel without a
        check-then-set race."""
        ...

    async def delete(self, key: str) -> None: ...
    @property
    def label(self) -> str: ...


class MemoryCache:
    """A tiny TTL cache backed by a dict. Not shared across processes."""

    def __init__(self, max_entries: int = 10_000) -> None:
        self._store: dict[str, tuple[float, str]] = {}
        self._max_entries = max_entries

    async def get(self, key: str) -> str | None:
        item = self._store.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= time.time():
            self._store.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: str, ttl: int) -> None:
        self._evict_expired_if_full()
        self._store[key] = (time.time() + ttl, value)

    async def add(self, key: str, value: str, ttl: int) -> bool:
        # Atomic w.r.t. the event loop: there is no ``await`` between the
        # liveness check and the write, so no other coroutine can interleave.
        now = time.time()
        item = self._store.get(key)
        if item is not None and item[0] >= now:
            return False
        self._evict_expired_if_full()
        self._store[key] = (now + ttl, value)
        return True

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def _evict_expired_if_full(self) -> None:
        # Drop expired entries before growing past the cap so the dict can't
        # accumulate keys that are never read again.
        if len(self._store) >= self._max_entries:
            now = time.time()
            for k in [k for k, (exp, _) in self._store.items() if exp <= now]:
                del self._store[k]

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

    async def add(self, key: str, value: str, ttl: int) -> bool:
        # SET key value NX EX ttl — atomic set-if-absent. Returns True when set.
        return bool(await self._client.set(key, value, ex=ttl, nx=True))

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    @property
    def label(self) -> str:
        return "redis"


class NullCache:
    """Disabled cache — always misses."""

    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, ttl: int) -> None:
        return None

    async def add(self, key: str, value: str, ttl: int) -> bool:
        # Nothing is stored, so every claim "succeeds" and idempotency is a
        # no-op (get always misses) — matching the disabled-cache contract.
        return True

    async def delete(self, key: str) -> None:
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
