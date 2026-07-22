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


def test_usage_by_client_capped_evicts_fewest_requests() -> None:
    m = Metrics(max_tracked_clients=3)
    # "hot" gets 5 requests; "warm" 2; "one-off" 1.
    for _ in range(5):
        m.record_client_usage("hot", tokens=1, cost_usd=None)
    for _ in range(2):
        m.record_client_usage("warm", tokens=1, cost_usd=None)
    m.record_client_usage("one-off", tokens=1, cost_usd=None)
    assert len(m.usage_by_client) == 3

    # A new client at the cap evicts the entry with the fewest requests.
    m.record_client_usage("newcomer", tokens=1, cost_usd=None)
    assert len(m.usage_by_client) == 3
    assert "one-off" not in m.usage_by_client
    assert "hot" in m.usage_by_client and "warm" in m.usage_by_client


def test_usage_by_client_existing_client_never_evicted_on_update() -> None:
    m = Metrics(max_tracked_clients=2)
    m.record_client_usage("a", tokens=1, cost_usd=None)
    m.record_client_usage("b", tokens=1, cost_usd=None)
    # Updating an already-tracked client at the cap must not evict anything.
    m.record_client_usage("a", tokens=1, cost_usd=None)
    assert set(m.usage_by_client) == {"a", "b"}


def test_usage_by_client_zero_cap_is_unlimited() -> None:
    m = Metrics(max_tracked_clients=0)
    for i in range(50):
        m.record_client_usage(f"c{i}", tokens=1, cost_usd=None)
    assert len(m.usage_by_client) == 50


def test_budget_window_cap_drops_stale_windows_first() -> None:
    m = Metrics(max_tracked_clients=2)
    # Two entries in window 10 (now=1000-1099, window=100).
    m.record_client_budget_usage("old-a", 0.10, window_seconds=100, now=1000.0)
    m.record_client_budget_usage("old-b", 0.20, window_seconds=100, now=1050.0)
    # A new client in the NEXT window: both stale entries are cleared, so the
    # live one is admitted without touching any current-window data.
    m.record_client_budget_usage("new", 0.05, window_seconds=100, now=1150.0)
    assert set(m._budget_window_usage) == {"new"}
    assert m.client_budget_window_cost("new", window_seconds=100, now=1160.0) == pytest.approx(0.05)


def test_budget_window_cap_evicts_cheapest_live_entry() -> None:
    m = Metrics(max_tracked_clients=2)
    m.record_client_budget_usage("big", 5.00, window_seconds=100, now=1000.0)
    m.record_client_budget_usage("small", 0.01, window_seconds=100, now=1010.0)
    # Same window, cap reached with live entries only: evict the cheapest.
    m.record_client_budget_usage("third", 1.00, window_seconds=100, now=1020.0)
    assert set(m._budget_window_usage) == {"big", "third"}


def test_seed_truncates_to_cap_keeping_busiest() -> None:
    m = Metrics(max_tracked_clients=2)
    m.seed(
        {
            "usage_by_client": {
                "busy": {"requests": 10, "tokens": 100, "cost_usd": 1.0},
                "medium": {"requests": 5, "tokens": 50, "cost_usd": 0.5},
                "idle": {"requests": 1, "tokens": 1, "cost_usd": 0.0},
            }
        }
    )
    assert set(m.usage_by_client) == {"busy", "medium"}


def test_create_app_applies_max_tracked_clients() -> None:
    from rekai.metrics import metrics as global_metrics

    create_app(
        Settings(
            environment="test",
            default_provider="echo",
            rate_limit_enabled=False,
            max_tracked_clients=123,
        )
    )
    assert global_metrics.max_tracked_clients == 123
    # Restore the default so other tests aren't affected.
    global_metrics.max_tracked_clients = 10_000


def test_client_cost_usd_reads_accumulated_spend() -> None:
    m = Metrics()
    assert m.client_cost_usd("key:never-seen") == 0.0
    m.record_client_usage("key:aaa", tokens=10, cost_usd=0.01)
    m.record_client_usage("key:aaa", tokens=5, cost_usd=0.02)
    assert m.client_cost_usd("key:aaa") == pytest.approx(0.03)


def test_record_client_budget_usage_accumulates_within_window() -> None:
    m = Metrics()
    m.record_client_budget_usage("key:aaa", 0.10, window_seconds=100, now=1000.0)
    m.record_client_budget_usage("key:aaa", 0.20, window_seconds=100, now=1050.0)
    assert m.client_budget_window_cost("key:aaa", window_seconds=100, now=1090.0) == pytest.approx(
        0.30
    )


def test_record_client_budget_usage_resets_on_window_rollover() -> None:
    m = Metrics()
    m.record_client_budget_usage("key:aaa", 5.0, window_seconds=100, now=1000.0)  # window 10
    assert m.client_budget_window_cost("key:aaa", window_seconds=100, now=1005.0) == 5.0
    # A new request arrives in the next window (11) — its cost should not
    # carry the previous window's spend forward.
    m.record_client_budget_usage("key:aaa", 1.0, window_seconds=100, now=1105.0)
    assert m.client_budget_window_cost("key:aaa", window_seconds=100, now=1105.0) == 1.0
    # Merely reading (no new spend) after rollover also reports 0, not stale spend.
    m2 = Metrics()
    m2.record_client_budget_usage("key:bbb", 5.0, window_seconds=100, now=1000.0)
    assert m2.client_budget_window_cost("key:bbb", window_seconds=100, now=1200.0) == 0.0


def test_client_budget_window_cost_unset_client_returns_zero() -> None:
    m = Metrics()
    assert m.client_budget_window_cost("key:never-seen", window_seconds=100, now=1000.0) == 0.0


def test_record_client_budget_usage_tolerates_none_and_zero_cost() -> None:
    m = Metrics()
    m.record_client_budget_usage("key:ccc", None, window_seconds=100, now=1000.0)
    m.record_client_budget_usage("key:ccc", 0.0, window_seconds=100, now=1000.0)
    assert m.client_budget_window_cost("key:ccc", window_seconds=100, now=1000.0) == 0.0


def test_seed_resets_budget_window_usage() -> None:
    m = Metrics()
    m.record_client_budget_usage("key:aaa", 5.0, window_seconds=100, now=1000.0)
    m.seed({})
    assert m.client_budget_window_cost("key:aaa", window_seconds=100, now=1000.0) == 0.0


def test_snapshot_excludes_budget_window_usage() -> None:
    m = Metrics()
    m.record_client_budget_usage("key:aaa", 5.0, window_seconds=100, now=1000.0)
    assert "_budget_window_usage" not in m.snapshot()
    assert "budget_window_usage" not in m.snapshot()


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
    def __init__(self, baseline: dict | None, others: list[dict] | None = None) -> None:
        self.baseline = baseline
        self.saved: dict | None = None
        self.others = others or []

    async def load(self) -> dict | None:
        return self.baseline

    async def save(self, snapshot: dict) -> None:
        self.saved = snapshot

    async def load_others(self) -> list[dict]:
        return self.others


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


def test_merge_snapshots_sums_scalars_providers_and_clients() -> None:
    from rekai.metrics import merge_snapshots

    a = {
        "requests_total": 5,
        "tokens_total": 10,
        "cost_usd_total": 0.25,
        "requests_by_provider": {"echo": 5},
        "usage_by_client": {"key:local": {"requests": 5, "tokens": 10, "cost_usd": 0.25}},
    }
    b = {
        "requests_total": 100,
        "tokens_total": 500,
        "cost_usd_total": 1.5,
        "requests_by_provider": {"echo": 20, "openai": 80},
        "usage_by_client": {"key:peer": {"requests": 100, "tokens": 500, "cost_usd": 1.5}},
    }
    merged = merge_snapshots([a, b])
    assert merged["requests_total"] == 105
    assert merged["tokens_total"] == 510
    assert merged["cost_usd_total"] == 1.75
    assert merged["requests_by_provider"] == {"echo": 25, "openai": 80}
    assert set(merged["usage_by_client"]) == {"key:local", "key:peer"}


def test_merge_snapshots_caps_to_busiest_clients() -> None:
    from rekai.metrics import merge_snapshots

    snap = {
        "usage_by_client": {
            "a": {"requests": 1, "tokens": 0, "cost_usd": 0.0},
            "b": {"requests": 9, "tokens": 0, "cost_usd": 0.0},
            "c": {"requests": 5, "tokens": 0, "cost_usd": 0.0},
        }
    }
    merged = merge_snapshots([snap], cap=2)
    # Keeps the two busiest by request count (b, c); drops the quietest (a).
    assert set(merged["usage_by_client"]) == {"b", "c"}


def test_usage_endpoint_aggregates_peer_snapshots(monkeypatch) -> None:
    # A second replica's persisted snapshot is summed with this instance's live
    # counters so /v1/usage reflects the whole fleet, not just one process.
    peer = {
        "requests_total": 100,
        "tokens_total": 500,
        "cost_usd_total": 1.5,
        "requests_by_provider": {"openai": 100},
        "usage_by_client": {"key:peer": {"requests": 100, "tokens": 500, "cost_usd": 1.5}},
    }
    fake = _FakeStore(baseline=None, others=[peer])
    monkeypatch.setattr(main_module, "build_metrics_store", lambda settings: fake)

    saved = main_module.metrics.snapshot()
    try:
        main_module.metrics.seed(
            {
                "requests_total": 5,
                "tokens_total": 10,
                "cost_usd_total": 0.25,
                "requests_by_provider": {"echo": 5},
                "usage_by_client": {"key:local": {"requests": 5, "tokens": 10, "cost_usd": 0.25}},
            }
        )
        client = TestClient(
            create_app(
                Settings(environment="test", default_provider="echo", rate_limit_enabled=False)
            )
        )
        body = client.get("/v1/usage").json()
        assert body["requests_total"] == 105
        assert body["tokens_total"] == 510
        assert body["cost_usd_total"] == 1.75
        assert body["requests_by_provider"] == {"echo": 5, "openai": 100}
        assert set(body["usage_by_client"]) == {"key:local", "key:peer"}
    finally:
        main_module.metrics.seed(saved)


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
