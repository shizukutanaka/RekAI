"""Minimal Prometheus-style metrics with no external dependency."""

from __future__ import annotations

import threading


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
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
        per-tenant spend is observable without leaking the raw key anywhere."""
        with self._lock:
            usage = self.usage_by_client.setdefault(
                client_id, {"requests": 0, "tokens": 0, "cost_usd": 0.0}
            )
            usage["requests"] += 1
            usage["tokens"] += tokens
            if cost_usd:
                usage["cost_usd"] += cost_usd

    def seed(self, snapshot: dict) -> None:
        """Set counters from a persisted snapshot (used on startup)."""
        with self._lock:
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
            self.usage_by_client = {
                client: dict(usage) for client, usage in snapshot.get("usage_by_client", {}).items()
            }

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
