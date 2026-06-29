"""Optional persistence for the in-memory metrics counters.

Uses a write-behind strategy: the live counters stay in memory (fast, no I/O on
the request path); a baseline is loaded from Redis on startup and the snapshot is
flushed back periodically and on shutdown. When no Redis URL is configured the
store is a no-op and metrics are simply process-local.
"""

from __future__ import annotations

import json
from typing import Protocol

from rekai.config import Settings
from rekai.logging_config import get_logger

logger = get_logger("rekai.metrics_store")

_KEY = "rekai:metrics:snapshot"


class MetricsStore(Protocol):
    async def load(self) -> dict | None: ...
    async def save(self, snapshot: dict) -> None: ...


class NullMetricsStore:
    async def load(self) -> dict | None:
        return None

    async def save(self, snapshot: dict) -> None:
        return None


class RedisMetricsStore:
    def __init__(self, url: str) -> None:
        import redis.asyncio as redis

        self._client = redis.from_url(url, decode_responses=True)

    async def load(self) -> dict | None:
        try:
            raw = await self._client.get(_KEY)
        except Exception as exc:  # pragma: no cover - network/redis failure
            logger.warning("could not load metrics snapshot: %s", exc)
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def save(self, snapshot: dict) -> None:
        try:
            await self._client.set(_KEY, json.dumps(snapshot))
        except Exception as exc:  # pragma: no cover - network/redis failure
            logger.warning("could not persist metrics snapshot: %s", exc)


def build_metrics_store(settings: Settings) -> MetricsStore:
    if settings.redis_url:
        try:
            return RedisMetricsStore(settings.redis_url)
        except Exception:  # pragma: no cover - redis client init failure
            return NullMetricsStore()
    return NullMetricsStore()
