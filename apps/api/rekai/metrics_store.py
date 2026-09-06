"""Optional persistence for the in-memory metrics counters.

Uses a write-behind strategy: the live counters stay in memory (fast, no I/O on
the request path); a baseline is loaded from Redis on startup and the snapshot is
flushed back periodically and on shutdown. When no Redis URL is configured the
store is a no-op and metrics are simply process-local.

Multi-replica aware: each replica persists to its own key
``rekai:metrics:snapshot:<instance-id>``. A replica loads only *its own* prior
snapshot as its startup baseline, while the ``/v1/usage`` read path sums this
instance's live counters with every *other* replica's persisted snapshot
(:meth:`MetricsStore.load_others`) for a fleet-wide view. The per-instance
``/metrics`` endpoint stays un-aggregated so a Prometheus scraper — which
already sums across scraped targets — doesn't double-count.

The instance id is a fresh uuid per process unless ``REKAI_INSTANCE_ID`` is set,
which is deliberate: uvicorn workers share a host, so anything host-derived
would collapse N workers onto one key and undercount by a factor of N. The
consequence is that **the startup baseline is only restored when
``REKAI_INSTANCE_ID`` is set** — with a generated id a restart always begins at
zero and its previous run is picked up as a "peer" instead. Fleet totals come
out the same either way; only which side of the sum they arrive on differs.

That also means a process start leaves a snapshot behind that nothing will ever
overwrite, so keys carry a TTL (:data:`_SNAPSHOT_TTL_SECONDS`), refreshed on
every flush. Without it each restart leaked a permanent key *and* a permanent
per-request cost: ``load_others`` runs on every ``/v1/usage`` call and does one
GET per key (measured against a local Redis: 200 restarts → 200 keys, all with
``TTL -1``, 194 ms per call, growing linearly and never shrinking).
"""

from __future__ import annotations

import json
import uuid
from typing import Protocol

from rekai.config import Settings
from rekai.logging_config import get_logger

logger = get_logger("rekai.metrics_store")

_PREFIX = "rekai:metrics:snapshot:"

#: How long a snapshot outlives its last flush. A live replica rewrites its key
#: every ``metrics_persist_interval_seconds``, so this only ever collects one
#: that has stopped flushing — replaced by a deploy, or restarted with a fresh
#: generated id. A day is long enough that a replica down for maintenance still
#: shows up in the fleet total when it returns, and short enough that restarts
#: cannot accumulate without bound.
_SNAPSHOT_TTL_SECONDS = 24 * 60 * 60


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
    def __init__(
        self, url: str, instance_id: str, ttl_seconds: int = _SNAPSHOT_TTL_SECONDS
    ) -> None:
        import redis.asyncio as redis

        self._client = redis.from_url(url, decode_responses=True)
        self._instance_id = instance_id
        self._key = _PREFIX + instance_id
        self._ttl = ttl_seconds

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
            # The TTL is set on every flush, so a live replica's key never
            # expires under it; only one that stopped flushing does.
            await self._client.set(self._key, json.dumps(snapshot), ex=self._ttl)
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
        # Floor the TTL at several flush intervals so an operator who sets a very
        # long persist interval can't configure a key that expires between its
        # own flushes.
        ttl = max(_SNAPSHOT_TTL_SECONDS, settings.metrics_persist_interval_seconds * 3)
        try:
            return RedisMetricsStore(settings.redis_url, instance_id, ttl)
        except Exception:  # pragma: no cover - redis client init failure
            return NullMetricsStore()
    return NullMetricsStore()
