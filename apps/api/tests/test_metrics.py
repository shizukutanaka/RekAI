"""Tests for metrics counters and persistence (write-behind)."""

from __future__ import annotations

from fastapi.testclient import TestClient

import rekai.main as main_module
from rekai.config import Settings
from rekai.main import create_app
from rekai.metrics import Metrics
from rekai.metrics_store import NullMetricsStore


def test_seed_sets_absolute_values() -> None:
    m = Metrics()
    m.record_request("echo")  # 1
    snapshot = {
        "requests_total": 42,
        "cache_hits_total": 5,
        "tokens_total": 100,
        "cost_usd_total": 1.5,
        "requests_by_provider": {"openai": 42},
    }
    m.seed(snapshot)
    out = m.snapshot()
    assert out["requests_total"] == 42
    assert out["cache_hits_total"] == 5
    assert out["tokens_total"] == 100
    assert out["cost_usd_total"] == 1.5
    assert out["requests_by_provider"] == {"openai": 42}


def test_snapshot_seed_roundtrip() -> None:
    m = Metrics()
    m.record_request("echo")
    m.record_tokens(10)
    m.record_cost(0.25)
    m.record_retry()
    m.record_cooldown()
    snap = m.snapshot()
    m2 = Metrics()
    m2.seed(snap)
    assert m2.snapshot() == snap


def test_retry_and_cooldown_counters() -> None:
    m = Metrics()
    m.record_retry()
    m.record_retry()
    m.record_cooldown()
    snap = m.snapshot()
    assert snap["retries_total"] == 2
    assert snap["cooldowns_total"] == 1
    # Surfaced in the Prometheus exposition too.
    text = m.render()
    assert "rekai_retries_total 2" in text
    assert "rekai_cooldowns_total 1" in text


async def test_null_store_is_noop() -> None:
    store = NullMetricsStore()
    assert await store.load() is None
    await store.save({"requests_total": 1})  # must not raise


class _FakeStore:
    def __init__(self, baseline: dict | None) -> None:
        self.baseline = baseline
        self.saved: dict | None = None

    async def load(self) -> dict | None:
        return self.baseline

    async def save(self, snapshot: dict) -> None:
        self.saved = snapshot


def test_lifespan_seeds_and_persists(monkeypatch) -> None:
    baseline = {"requests_total": 1000, "requests_by_provider": {"echo": 1000}}
    fake = _FakeStore(baseline)
    monkeypatch.setattr(main_module, "build_metrics_store", lambda settings: fake)

    settings = Settings(rate_limit_enabled=False, metrics_persist_interval_seconds=3600)
    try:
        # Using the client as a context manager runs the lifespan (startup/shutdown).
        with TestClient(create_app(settings)) as client:
            usage = client.get("/v1/usage").json()
            assert usage["requests_total"] >= 1000  # seeded baseline applied
        # On shutdown the snapshot is flushed back to the store.
        assert fake.saved is not None
        assert fake.saved["requests_total"] >= 1000
    finally:
        # Restore the shared singleton so other tests are unaffected.
        main_module.metrics.seed({})
