"""Ops alert webhook: discrete events reach REKAI_ALERT_WEBHOOK_URL once each.

Alerting must never break a request: delivery is fire-and-forget, failures
are logged-and-dropped, and a (event, subject) pair alerts at most once per
dedupe window so a retry storm can't spam the hook.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from rekai import alerts
from rekai.config import Settings
from rekai.main import create_app
from rekai.providers import register_provider
from rekai.providers.base import Provider, ProviderError

WEBHOOK = "https://hooks.example.test/xyz"


@pytest.fixture(autouse=True)
def _fresh_dedup():
    alerts._recent.clear()
    yield
    alerts._recent.clear()


@pytest.fixture
def captured(monkeypatch):
    """Replace delivery with a synchronous capture — notify() then behaves
    deterministically inside TestClient requests too."""
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(alerts, "_deliver", lambda url, payload: sent.append((url, payload)))
    return sent


def _settings(**kw) -> Settings:
    kw.setdefault("environment", "test")
    kw.setdefault("default_provider", "echo")
    return Settings(**kw)


def test_no_webhook_delivers_nothing(captured) -> None:
    alerts.notify(_settings(), "budget_exceeded", "client-1")
    assert captured == []


def test_payload_shape(captured) -> None:
    alerts.notify(
        _settings(alert_webhook_url=WEBHOOK),
        "budget_exceeded",
        "client-1",
        {"budget_usd": 5.0},
    )
    (url, payload), *_ = captured
    assert url == WEBHOOK
    assert payload["event"] == "budget_exceeded"
    assert payload["subject"] == "client-1"
    assert payload["detail"] == {"budget_usd": 5.0}
    assert payload["source"] == "rekai"
    assert payload["timestamp"] > 0


def test_same_subject_is_deduped(captured) -> None:
    s = _settings(alert_webhook_url=WEBHOOK)
    alerts.notify(s, "budget_exceeded", "client-1")
    alerts.notify(s, "budget_exceeded", "client-1")
    assert len(captured) == 1


def test_distinct_subjects_alert_independently(captured) -> None:
    s = _settings(alert_webhook_url=WEBHOOK)
    alerts.notify(s, "budget_exceeded", "client-1")
    alerts.notify(s, "budget_exceeded", "client-2")
    alerts.notify(s, "provider_parked", "client-1")  # different event too
    assert len(captured) == 3


def test_notify_is_safe_outside_an_event_loop() -> None:
    """Called from a sync context (e.g. a non-async call site), it's a no-op —
    alerting must never raise."""
    alerts.notify(_settings(alert_webhook_url=WEBHOOK), "x", "y")


# --- the events themselves ----------------------------------------------------


def test_budget_exceeded_fires_the_hook(captured) -> None:
    app = create_app(_settings(alert_webhook_url=WEBHOOK, client_budget_usd=0.0))
    resp = TestClient(app).post(
        "/v1/chat",
        json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 402
    (url, payload), *_ = captured
    assert payload["event"] == "budget_exceeded"
    assert payload["detail"]["budget_usd"] == 0.0


class _FailingProvider(Provider):
    name = "flaky9"
    requires_key = False

    async def chat(self, request, api_key):  # type: ignore[no-untyped-def]
        raise ProviderError("rate limited", 429)

    async def stream_events(  # type: ignore[no-untyped-def]
        self, request, api_key
    ) -> AsyncIterator:
        raise ProviderError("rate limited", 429)
        yield  # pragma: no cover - makes this an async generator


def test_provider_park_fires_the_hook(captured) -> None:
    register_provider(_FailingProvider())
    app = create_app(_settings(alert_webhook_url=WEBHOOK, default_provider="flaky9"))
    resp = TestClient(app).post(
        "/v1/chat",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 429  # the parked provider's upstream error passes through
    (url, payload), *_ = captured
    assert payload["event"] == "provider_parked"
    assert payload["subject"] == "flaky9"
    assert payload["detail"]["status"] == 429


# --- delivery is best-effort --------------------------------------------------


async def test_delivery_failure_is_logged_not_raised(monkeypatch) -> None:
    class _BoomClient:
        is_closed = False

        async def post(self, *a, **k):
            raise RuntimeError("hook endpoint down")

    monkeypatch.setattr(alerts, "_client", lambda: _BoomClient())
    await alerts._post(WEBHOOK, {"event": "x"})


async def test_delivery_4xx_is_logged_not_raised(monkeypatch) -> None:
    class _Resp:
        status_code = 500

    class _BadClient:
        is_closed = False

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(alerts, "_client", lambda: _BadClient())
    await alerts._post(WEBHOOK, {"event": "x"})
