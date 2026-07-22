"""Optional persistence for the in-memory metrics counters.

Uses a write-behind strategy: the live counters stay in memory (fast, no I/O on
the request path); a baseline is loaded from Redis on startup and the snapshot is
flushed back periodically and on shutdown. When no Redis URL is configured the
store is a no-op and metrics are simply process-local.

Multi-replica aware: each replica persists to its own key
``rekai:metrics:snapshot:<instance-id>``. A replica loads only *its own* prior
snapshot as its startup baseline (so a restart resumes where it left off without
double-counting its peers), while the ``/v1/usage`` read path sums this
instance's live counters with every *other* replica's persisted snapshot
(:meth:`MetricsStore.load_others`) for a fleet-wide view. The per-instance
``/metrics`` endpoint stays un-aggregated so a Prometheus scraper — which
already sums across scraped targets — doesn't double-count.
"""

from __future__ import annotations

import json
import uuid
from typing import Protocol

from rekai.config import Settings
from rekai.logging_config import get_logger

logger = get_logger("rekai.metrics_store")

_PREFIX = "rekai:metrics:snapshot:"


class MetricsStore(Protocol):
    async def load(self) -> dict | None: ...
    async def save(self, snapshot: dict) -> None: ...
    async def load_others(self) -> list[dict]:
        """Persisted snapshots of every *other* replica (empty when process-local
        or on a backend error) — summed with the local live counters for the
        aggregate ``/v1/usage`` view."""
        ...


class NullMetricsStore:
    async def load(self) -> dict | None:
        return None

    async def save(self, snapshot: dict) -> None:
        return None

    async def load_others(self) -> list[dict]:
        return []


class RedisMetricsStore:
    def __init__(self, url: str, instance_id: str) -> None:
        import redis.asyncio as redis

        self._client = redis.from_url(url, decode_responses=True)
        self._instance_id = instance_id
        self._key = _PREFIX + instance_id

    async def load(self) -> dict | None:
        try:
            raw = await self._client.get(self._key)
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
            await self._client.set(self._key, json.dumps(snapshot))
        except Exception as exc:  # pragma: no cover - network/redis failure
            logger.warning("could not persist metrics snapshot: %s", exc)

    async def load_others(self) -> list[dict]:
        snapshots: list[dict] = []
        try:
            async for key in self._client.scan_iter(match=_PREFIX + "*"):
                if key == self._key:
                    continue  # the caller adds the fresher live local snapshot
                raw = await self._client.get(key)
                if not raw:
                    continue
                try:
                    snapshots.append(json.loads(raw))
                except json.JSONDecodeError:  # pragma: no cover - defensive
                    continue
        except Exception as exc:  # pragma: no cover - fail open on backend error
            logger.warning("could not load peer metrics snapshots: %s", exc)
            return []
        return snapshots


def build_metrics_store(settings: Settings) -> MetricsStore:
    if settings.redis_url:
        instance_id = settings.instance_id or uuid.uuid4().hex[:12]
        try:
            return RedisMetricsStore(settings.redis_url, instance_id)
        except Exception:  # pragma: no cover - redis client init failure
            return NullMetricsStore()
    return NullMetricsStore()
