"""Minimal Prometheus-style metrics with no external dependency."""

from __future__ import annotations

import threading


class Metrics:
    def __init__(self, max_tracked_clients: int = 10_000) -> None:
        self._lock = threading.Lock()
        # Cap on distinct client ids kept in usage_by_client and
        # _budget_window_usage (0 = unlimited). Without gateway auth the client
        # id is the raw request IP, so an unbounded dict is a slow memory leak
        # on any internet-facing deployment — the RateLimiter caps its buckets
        # for the same reason. create_app() overrides this from
        # REKAI_MAX_TRACKED_CLIENTS.
        self.max_tracked_clients = max_tracked_clients
        self.requests_total = 0
        self.cache_hits_total = 0
        self.cache_misses_total = 0
        self.errors_total = 0
        self.fallbacks_total = 0
        self.retries_total = 0
        self.cooldowns_total = 0
        self.tokens_total = 0
        self.cost_usd_total = 0.0
        self.requests_by_provider: dict[str, int] = {}
        # Per-tenant usage, keyed by the masked "key:<hash>" client id (or the
        # client IP when the gateway has no auth configured).
        self.usage_by_client: dict[str, dict[str, float]] = {}
        # Per-client cost within the *current* budget window only (see
        # record_client_budget_usage) — client_id -> (window_index, cost in
        # that window). Deliberately separate from usage_by_client (lifetime,
        # for /v1/usage and /metrics) and deliberately NOT part of
        # seed()/snapshot(): a window index only means something relative to
        # a `now`, and reconciling that across a restart gap of unknown
        # length isn't worth the complexity for an enforcement-only
        # structure. A restart simply starts the current window at $0 — the
        # same "approximate, not billing" tradeoff already applied elsewhere
        # (rekai/pricing.py's estimates; budget enforcement in general is
        # already process-local best-effort across workers even without
        # this feature — see docs/architecture.md).
        self._budget_window_usage: dict[str, tuple[int, float]] = {}

    def record_request(self, provider: str) -> None:
        with self._lock:
            self.requests_total += 1
            self.requests_by_provider[provider] = self.requests_by_provider.get(provider, 0) + 1

    def record_cache(self, hit: bool) -> None:
        with self._lock:
            if hit:
                self.cache_hits_total += 1
            else:
                self.cache_misses_total += 1

    def record_error(self) -> None:
        with self._lock:
            self.errors_total += 1

    def record_fallback(self) -> None:
        with self._lock:
            self.fallbacks_total += 1

    def record_retry(self) -> None:
        with self._lock:
            self.retries_total += 1

    def record_cooldown(self) -> None:
        with self._lock:
            self.cooldowns_total += 1

    def record_tokens(self, count: int) -> None:
        with self._lock:
            self.tokens_total += count

    def record_cost(self, cost_usd: float | None) -> None:
        if cost_usd:
            with self._lock:
                self.cost_usd_total += cost_usd

    def record_client_usage(self, client_id: str, tokens: int, cost_usd: float | None) -> None:
        """Attribute one request's tokens/cost to a client (API key or IP), so
        per-tenant spend is observable without leaking the raw key anywhere.

        Bounded by ``max_tracked_clients``: admitting a new client at the cap
        evicts the tracked client with the fewest requests, so one-off IPs
        churn while active tenants stay. Eviction also resets that client's
        lifetime-budget baseline (client_cost_usd starts over) — acceptable
        because budget enforcement is documented as approximate, and the
        alternative is unbounded growth."""
        with self._lock:
            usage = self.usage_by_client.get(client_id)
            if usage is None:
                cap = self.max_tracked_clients
                if cap and len(self.usage_by_client) >= cap:
                    coldest = min(
                        self.usage_by_client, key=lambda c: self.usage_by_client[c]["requests"]
                    )
                    del self.usage_by_client[coldest]
                usage = {"requests": 0, "tokens": 0, "cost_usd": 0.0}
                self.usage_by_client[client_id] = usage
            usage["requests"] += 1
            usage["tokens"] += tokens
            if cost_usd:
                usage["cost_usd"] += cost_usd

    def client_cost_usd(self, client_id: str) -> float:
        """Read a client's cumulative cost so far (0.0 if never recorded)."""
        with self._lock:
            usage = self.usage_by_client.get(client_id)
            return usage["cost_usd"] if usage else 0.0

    def record_client_budget_usage(
        self, client_id: str, cost_usd: float | None, window_seconds: int, now: float
    ) -> None:
        """Attribute cost to a client within the current fixed budget window
        (REKAI_CLIENT_BUDGET_WINDOW_SECONDS), separate from usage_by_client's
        lifetime total. ``now`` (typically ``time.time()``) is supplied by the
        caller rather than read internally, so tests can exercise window
        rollover deterministically without a clock dependency on Metrics."""
        if not cost_usd:
            return
        with self._lock:
            window_index = int(now / window_seconds)
            prev = self._budget_window_usage.get(client_id)
            cap = self.max_tracked_clients
            if prev is None and cap and len(self._budget_window_usage) >= cap:
                # Entries from past windows are dead weight (reads return 0.0
                # for them), so clear those first; only evict a live entry —
                # the cheapest one — if the cap is still exceeded.
                stale = [c for c, (w, _) in self._budget_window_usage.items() if w != window_index]
                for c in stale:
                    del self._budget_window_usage[c]
                if len(self._budget_window_usage) >= cap:
                    cheapest = min(
                        self._budget_window_usage,
                        key=lambda c: self._budget_window_usage[c][1],
                    )
                    del self._budget_window_usage[cheapest]
            base = prev[1] if prev is not None and prev[0] == window_index else 0.0
            self._budget_window_usage[client_id] = (window_index, base + cost_usd)

    def client_budget_window_cost(self, client_id: str, window_seconds: int, now: float) -> float:
        """Read a client's cost accumulated in the current fixed window (0.0
        if never recorded, including immediately after a rollover)."""
        with self._lock:
            entry = self._budget_window_usage.get(client_id)
            if entry is None:
                return 0.0
            return entry[1] if entry[0] == int(now / window_seconds) else 0.0

    def seed(self, snapshot: dict) -> None:
        """Set counters from a persisted snapshot (used on startup)."""
        with self._lock:
            self._budget_window_usage = {}
            self.requests_total = snapshot.get("requests_total", 0)
            self.cache_hits_total = snapshot.get("cache_hits_total", 0)
            self.cache_misses_total = snapshot.get("cache_misses_total", 0)
            self.errors_total = snapshot.get("errors_total", 0)
            self.fallbacks_total = snapshot.get("fallbacks_total", 0)
            self.retries_total = snapshot.get("retries_total", 0)
            self.cooldowns_total = snapshot.get("cooldowns_total", 0)
            self.tokens_total = snapshot.get("tokens_total", 0)
            self.cost_usd_total = snapshot.get("cost_usd_total", 0.0)
            self.requests_by_provider = dict(snapshot.get("requests_by_provider", {}))
            clients = snapshot.get("usage_by_client", {})
            # A snapshot persisted before the cap existed (or under a larger
            # one) may exceed max_tracked_clients — keep the busiest entries.
            cap = self.max_tracked_clients
            if cap and len(clients) > cap:
                kept = sorted(clients, key=lambda c: clients[c].get("requests", 0), reverse=True)
                clients = {c: clients[c] for c in kept[:cap]}
            self.usage_by_client = {client: dict(usage) for client, usage in clients.items()}

    def snapshot(self) -> dict:
        """Return a copy of the current counters as plain data."""
        with self._lock:
            return {
                "requests_total": self.requests_total,
                "cache_hits_total": self.cache_hits_total,
                "cache_misses_total": self.cache_misses_total,
                "errors_total": self.errors_total,
                "fallbacks_total": self.fallbacks_total,
                "retries_total": self.retries_total,
                "cooldowns_total": self.cooldowns_total,
                "tokens_total": self.tokens_total,
                "cost_usd_total": round(self.cost_usd_total, 6),
                "requests_by_provider": dict(self.requests_by_provider),
                "usage_by_client": {
                    client: dict(usage) for client, usage in self.usage_by_client.items()
                },
            }

    def render(self) -> str:
        """Render metrics in Prometheus text exposition format."""
        lines = [
            "# HELP rekai_requests_total Total chat requests handled.",
            "# TYPE rekai_requests_total counter",
            f"rekai_requests_total {self.requests_total}",
            "# HELP rekai_cache_hits_total Cache hits.",
            "# TYPE rekai_cache_hits_total counter",
            f"rekai_cache_hits_total {self.cache_hits_total}",
            "# HELP rekai_cache_misses_total Cache misses.",
            "# TYPE rekai_cache_misses_total counter",
            f"rekai_cache_misses_total {self.cache_misses_total}",
            "# HELP rekai_errors_total Errors returned to clients.",
            "# TYPE rekai_errors_total counter",
            f"rekai_errors_total {self.errors_total}",
            "# HELP rekai_fallbacks_total Times a fallback provider was attempted.",
            "# TYPE rekai_fallbacks_total counter",
            f"rekai_fallbacks_total {self.fallbacks_total}",
            "# HELP rekai_retries_total Transient upstream failures retried in place.",
            "# TYPE rekai_retries_total counter",
            f"rekai_retries_total {self.retries_total}",
            "# HELP rekai_cooldowns_total Times a provider was parked after a 429.",
            "# TYPE rekai_cooldowns_total counter",
            f"rekai_cooldowns_total {self.cooldowns_total}",
            "# HELP rekai_tokens_total Total tokens accounted across responses.",
            "# TYPE rekai_tokens_total counter",
            f"rekai_tokens_total {self.tokens_total}",
            "# HELP rekai_cost_usd_total Approximate cumulative USD cost.",
            "# TYPE rekai_cost_usd_total counter",
            f"rekai_cost_usd_total {round(self.cost_usd_total, 6)}",
        ]
        for provider, count in sorted(self.requests_by_provider.items()):
            lines.append(f'rekai_requests_total{{provider="{provider}"}} {count}')

        lines += [
            "# HELP rekai_client_requests_total Requests per client (API key or IP).",
            "# TYPE rekai_client_requests_total counter",
        ]
        for client, usage in sorted(self.usage_by_client.items()):
            lines.append(
                f'rekai_client_requests_total{{client="{client}"}} {int(usage["requests"])}'
            )
        lines += [
            "# HELP rekai_client_tokens_total Tokens accounted per client.",
            "# TYPE rekai_client_tokens_total counter",
        ]
        for client, usage in sorted(self.usage_by_client.items()):
            lines.append(f'rekai_client_tokens_total{{client="{client}"}} {int(usage["tokens"])}')
        lines += [
            "# HELP rekai_client_cost_usd_total Approximate cumulative USD cost per client.",
            "# TYPE rekai_client_cost_usd_total counter",
        ]
        for client, usage in sorted(self.usage_by_client.items()):
            cost = round(usage["cost_usd"], 6)
            lines.append(f'rekai_client_cost_usd_total{{client="{client}"}} {cost}')

        return "\n".join(lines) + "\n"


metrics = Metrics()
