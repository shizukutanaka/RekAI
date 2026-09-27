"""Fire-and-forget ops alerts: POST discrete events to a configured webhook.

A self-hosted gateway has no managed pager — an operator who isn't watching
`/metrics` never learns a provider parked itself or a client tripped its
budget. `REKAI_ALERT_WEBHOOK_URL` points at anything that accepts a JSON POST
(Slack/Discord incoming-webhook endpoints, a PagerDuty/ntfy bridge, a local
listener); RekAI posts `{"event", "subject", "detail", "timestamp"}`.

Alerting must never break a request: delivery runs as a detached task with a
short timeout, failures are logged and dropped, and a dedupe window means a
client hammering an over-budget key produces one alert per window, not one
per request.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import httpx

from rekai.logging_config import get_logger

if TYPE_CHECKING:
    from rekai.config import Settings

logger = get_logger("rekai.alerts")

_WEBHOOK_TIMEOUT = 5.0
#: Minimum seconds between two alerts for the same (event, subject) pair —
#: bounds webhook spam regardless of how often the condition re-trips.
_DEDUP_SECONDS = 300.0
_MAX_RECENT = 10_000

_client_per_loop: dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}
# (event, subject) -> monotonic time after which it may alert again.
_recent: dict[tuple[str, str], float] = {}


def _client() -> httpx.AsyncClient:
    """One persistent client per event loop (the Provider._client pattern):
    a loop-bound pool can't cross loops under pytest-asyncio."""
    loop = asyncio.get_running_loop()
    client = _client_per_loop.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT)
        _client_per_loop[loop] = client
    return client


async def _post(url: str, payload: dict[str, Any]) -> None:
    try:
        resp = await _client().post(url, json=payload)
        if resp.status_code >= 400:
            logger.warning("alert webhook %s answered %s", url, resp.status_code)
    except Exception as exc:
        # An alerting path that can raise is an alerting path that can take
        # down the request it rode in on — log and drop.
        logger.warning("alert webhook %s failed: %s", url, exc)


def _deduped(event: str, subject: str) -> bool:
    """True if this (event, subject) already alerted within the window."""
    now = time.monotonic()
    if _recent.get((event, subject), 0.0) > now:
        return True
    _recent[(event, subject)] = now + _DEDUP_SECONDS
    if len(_recent) > _MAX_RECENT:
        expired = [k for k, until in _recent.items() if until <= now]
        for k in expired:
            del _recent[k]
        # Still over the cap (a flood of *distinct* subjects): drop the oldest
        # arbitrary entries — alerts degrade rather than grow memory forever.
        while len(_recent) > _MAX_RECENT:
            _recent.pop(next(iter(_recent)))
    return False


def notify(
    settings: Settings,
    event: str,
    subject: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Queue a webhook POST unless the webhook is unset or this subject
    alerted recently. Safe to call from anywhere async code runs; a no-op
    where no event loop is running."""
    url = settings.alert_webhook_url
    if not url:
        return
    if _deduped(event, subject):
        return
    payload: dict[str, Any] = {
        "event": event,
        "subject": subject,
        "detail": detail or {},
        "timestamp": time.time(),
        "source": "rekai",
    }
    _deliver(url, payload)


def _deliver(url: str, payload: dict[str, Any]) -> None:
    """Schedule the POST on the running loop; a no-op in sync contexts."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    asyncio.create_task(_post(url, payload))
