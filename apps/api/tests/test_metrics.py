"""Tests for metrics counters and persistence (write-behind)."""

from __future__ import annotations

import pytest
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
    m.record_client_usage("key:abc123", 10, 0.25)
    snap = m.snapshot()
    m2 = Metrics()
    m2.seed(snap)
    assert m2.snapshot() == snap


def test_record_client_usage_accumulates_per_client() -> None:
    m = Metrics()
    m.record_client_usage("key:aaa", tokens=10, cost_usd=0.01)
    m.record_client_usage("key:aaa", tokens=5, cost_usd=0.02)
    m.record_client_usage("key:bbb", tokens=100, cost_usd=0.5)

    snap = m.snapshot()
    assert snap["usage_by_client"]["key:aaa"] == {"requests": 2, "tokens": 15, "cost_usd": 0.03}
    assert snap["usage_by_client"]["key:bbb"] == {"requests": 1, "tokens": 100, "cost_usd": 0.5}


def test_client_cost_usd_reads_accumulated_spend() -> None:
    m = Metrics()
    assert m.client_cost_usd("key:never-seen") == 0.0
    m.record_client_usage("key:aaa", tokens=10, cost_usd=0.01)
    m.record_client_usage("key:aaa", tokens=5, cost_usd=0.02)
    assert m.client_cost_usd("key:aaa") == pytest.approx(0.03)


def test_record_client_usage_tolerates_none_cost() -> None:
    m = Metrics()
    m.record_client_usage("key:ccc", tokens=3, cost_usd=None)
    assert m.snapshot()["usage_by_client"]["key:ccc"] == {
        "requests": 1,
        "tokens": 3,
        "cost_usd": 0.0,
    }


def test_client_usage_surfaced_in_prometheus_render() -> None:
    m = Metrics()
    m.record_client_usage("key:abc123", tokens=42, cost_usd=0.007)
    text = m.render()
    assert 'rekai_client_requests_total{client="key:abc123"} 1' in text
    assert 'rekai_client_tokens_total{client="key:abc123"} 42' in text
    assert 'rekai_client_cost_usd_total{client="key:abc123"} 0.007' in text


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


def test_usage_by_client_tracked_per_key() -> None:
    from rekai.auth import client_id

    settings = Settings(
        environment="test",
        default_provider="echo",
        api_keys="sk-usage-a,sk-usage-b",
        rate_limit_enabled=False,
    )
    client = TestClient(create_app(settings))
    try:
        body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}], "cache": False}
        client.post("/v1/chat", json=body, headers={"Authorization": "Bearer sk-usage-a"})
        client.post("/v1/chat", json=body, headers={"Authorization": "Bearer sk-usage-a"})
        client.post("/v1/chat", json=body, headers={"Authorization": "Bearer sk-usage-b"})

        # /v1/usage is also gated by gateway auth when keys are configured.
        usage = client.get("/v1/usage", headers={"Authorization": "Bearer sk-usage-a"}).json()[
            "usage_by_client"
        ]
        key_a, key_b = client_id("sk-usage-a"), client_id("sk-usage-b")
        assert usage[key_a]["requests"] == 2
        assert usage[key_b]["requests"] == 1
        assert usage[key_a]["tokens"] > 0
        # The raw keys never appear as dict keys — only their masked ids.
        assert "sk-usage-a" not in usage
        assert "sk-usage-b" not in usage
    finally:
        main_module.metrics.seed({})


def test_usage_by_client_survives_seed_accumulate_flush(monkeypatch) -> None:
    from rekai.auth import client_id

    preexisting = client_id("sk-usage-persisted")
    baseline = {
        "requests_total": 5,
        "usage_by_client": {preexisting: {"requests": 5, "tokens": 50, "cost_usd": 0.1}},
    }
    fake = _FakeStore(baseline)
    monkeypatch.setattr(main_module, "build_metrics_store", lambda settings: fake)

    settings = Settings(
        environment="test",
        default_provider="echo",
        api_keys="sk-usage-persisted,sk-usage-new",
        rate_limit_enabled=False,
        metrics_persist_interval_seconds=3600,
    )
    try:
        with TestClient(create_app(settings)) as client:
            # Seeded baseline is visible immediately on startup.
            usage = client.get(
                "/v1/usage", headers={"Authorization": "Bearer sk-usage-persisted"}
            ).json()["usage_by_client"]
            assert usage[preexisting]["requests"] == 5

            # A live request from a different client accumulates on top of the seed.
            body = {
                "model": "echo",
                "messages": [{"role": "user", "content": "hi"}],
                "cache": False,
            }
            client.post("/v1/chat", json=body, headers={"Authorization": "Bearer sk-usage-new"})

        # Shutdown flushes the merged state: the seeded client plus the new one.
        assert fake.saved is not None
        flushed = fake.saved["usage_by_client"]
        new_client = client_id("sk-usage-new")
        assert flushed[preexisting]["requests"] == 5
        assert flushed[new_client]["requests"] == 1
        assert flushed[new_client]["tokens"] > 0
    finally:
        main_module.metrics.seed({})
