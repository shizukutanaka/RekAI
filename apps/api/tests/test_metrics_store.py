"""Persisted metrics snapshots are bounded.

Every process start writes to ``rekai:metrics:snapshot:<instance-id>``, and the
instance id is a fresh uuid unless ``REKAI_INSTANCE_ID`` is set — deliberately,
since uvicorn workers share a host and anything host-derived would collapse N
workers onto one key. The consequence is that a restart never overwrites its
predecessor's key, so without an expiry every restart leaked one permanently.

Measured against a local Redis before the fix: 200 restarts left 200 keys, all
with ``TTL -1``, and ``load_others()`` — which runs on *every* ``/v1/usage``
request and does one GET per key — took 194 ms, growing linearly and never
shrinking. Totals stayed correct throughout; the cost was memory and latency,
and it violated the repo's own rule that anything keyed per client or per
instance carries a cap and eviction.
"""

from __future__ import annotations

import json

import pytest

from rekai.config import Settings
from rekai.metrics_store import (
    _PREFIX,
    _SNAPSHOT_TTL_SECONDS,
    NullMetricsStore,
    RedisMetricsStore,
    build_metrics_store,
)


class _FakeRedis:
    """Just enough of redis.asyncio for the metrics store: SET/GET/SCAN.

    ``expire_now`` drops a key the way a real TTL would, so a peer that stopped
    flushing can be simulated without waiting.
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value
        self.ttls[key] = ex

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def scan_iter(self, match: str):
        prefix = match.rstrip("*")
        for key in list(self.values):
            if key.startswith(prefix):
                yield key

    def expire_now(self, key: str) -> None:
        self.values.pop(key, None)
        self.ttls.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch) -> _FakeRedis:
    import redis.asyncio as redis

    fake = _FakeRedis()
    monkeypatch.setattr(redis, "from_url", lambda *a, **k: fake)
    return fake


# --- the expiry itself --------------------------------------------------------


async def test_save_sets_an_expiry(fake_redis) -> None:
    # The defect: SET with no `ex`, so the key lived forever (TTL -1).
    store = RedisMetricsStore("redis://unused", "instance-a")

    await store.save({"requests": 1})

    assert fake_redis.ttls[_PREFIX + "instance-a"] == _SNAPSHOT_TTL_SECONDS


async def test_every_flush_refreshes_the_expiry(fake_redis) -> None:
    # A live replica rewrites its key each persist interval, so the TTL can
    # never catch up with it — only a replica that stopped flushing expires.
    store = RedisMetricsStore("redis://unused", "instance-a", ttl_seconds=600)

    await store.save({"requests": 1})
    fake_redis.ttls[_PREFIX + "instance-a"] = 5  # the clock advances
    await store.save({"requests": 2})

    assert fake_redis.ttls[_PREFIX + "instance-a"] == 600


async def test_expired_peer_drops_out_of_the_aggregate(fake_redis) -> None:
    live = RedisMetricsStore("redis://unused", "live")
    retired = RedisMetricsStore("redis://unused", "retired")
    await live.save({"requests": 10})
    await retired.save({"requests": 7})

    reader = RedisMetricsStore("redis://unused", "reader")
    assert sorted(s["requests"] for s in await reader.load_others()) == [7, 10]

    fake_redis.expire_now(_PREFIX + "retired")

    assert [s["requests"] for s in await reader.load_others()] == [10]


# --- the TTL a real deployment gets -------------------------------------------


def test_ttl_defaults_to_a_day(fake_redis) -> None:
    store = build_metrics_store(Settings(redis_url="redis://unused"))

    assert isinstance(store, RedisMetricsStore)
    assert store._ttl == _SNAPSHOT_TTL_SECONDS == 24 * 60 * 60


def test_ttl_is_floored_at_several_flush_intervals(fake_redis) -> None:
    # An operator who flushes less often than the TTL would otherwise configure
    # a key that expires between its own writes.
    store = build_metrics_store(
        Settings(redis_url="redis://unused", metrics_persist_interval_seconds=60 * 60 * 24 * 2)
    )

    assert isinstance(store, RedisMetricsStore)
    assert store._ttl == 60 * 60 * 24 * 6


def test_no_redis_means_no_store() -> None:
    assert isinstance(build_metrics_store(Settings(redis_url=None)), NullMetricsStore)


# --- contracts the expiry must not have broken --------------------------------


async def test_own_snapshot_round_trips(fake_redis) -> None:
    store = RedisMetricsStore("redis://unused", "instance-a")

    await store.save({"requests": 42})

    loaded = await store.load()
    assert loaded is not None and loaded["requests"] == 42


async def test_load_others_excludes_this_instance(fake_redis) -> None:
    # The caller adds the fresher live local counters, so including the
    # instance's own persisted snapshot here would double-count it.
    store = RedisMetricsStore("redis://unused", "mine")
    await store.save({"requests": 5})
    await RedisMetricsStore("redis://unused", "theirs").save({"requests": 3})

    assert [s["requests"] for s in await store.load_others()] == [3]


async def test_a_corrupt_snapshot_is_skipped_not_fatal(fake_redis) -> None:
    await RedisMetricsStore("redis://unused", "good").save({"requests": 1})
    fake_redis.values[_PREFIX + "bad"] = "{not json"

    store = RedisMetricsStore("redis://unused", "reader")

    assert [s["requests"] for s in await store.load_others()] == [1]
    assert json.loads(fake_redis.values[_PREFIX + "good"])["requests"] == 1
