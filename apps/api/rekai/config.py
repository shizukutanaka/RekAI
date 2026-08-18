"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings.

    All values can be overridden through environment variables (or a local
    ``.env`` file). See ``.env.example`` for the full list.
    """

    model_config = SettingsConfigDict(env_prefix="REKAI_", env_file=".env", extra="ignore")

    # General
    app_name: str = "RekAI"
    # "production" is not a label: it makes the gateway refuse to start in a
    # configuration that would turn it into an open proxy for your own provider
    # keys (see `open_proxy_hazard`). Anything else only warns.
    environment: str = "development"
    log_level: str = "INFO"

    # Gateway auth: comma-separated client API keys. When set, /v1/* requires
    # `Authorization: Bearer <key>`. Empty = open (no client auth).
    api_keys: str = ""

    # Runtime-managed keys on top of the static list above, added/revoked via
    # the admin API without a redeploy. Stored in the configured cache backend
    # (Redis if set, else process-local) — see rekai/keystore.py.
    dynamic_keys_enabled: bool = False

    # Optional Fernet key (rekai.security.generate_key()) to encrypt dynamic
    # keys at rest — they're persisted (unlike transient BYOK keys), so an
    # operator using a shared Redis they don't fully trust may want this.
    # Unset (default) = stored as plaintext, same as before this existed.
    dynamic_keys_encryption_key: str | None = None

    # Shared secret for /admin/* (key management). Unset (default) = the admin
    # API isn't registered at all. Distinct from api_keys: an admin credential,
    # not a tenant one.
    admin_key: str | None = None

    # /admin/* has no per-tenant identity (one shared secret for the whole
    # deployment), so this is checked *before* the admin-key check, keyed by
    # client IP — unlike the tenant gateway auth gate, where checking auth
    # first is the point (so a guesser can't burn a real tenant's budget).
    # Here, every attempt (right or wrong key) should count, since the threat
    # is brute-forcing the one shared secret. Only active when admin_key is set.
    admin_rate_limit_enabled: bool = True
    admin_rate_limit_requests: int = Field(default=20, ge=1)
    admin_rate_limit_window_seconds: int = Field(default=60, ge=1)

    # Prompt-injection guardrail (OWASP LLM01). Heuristic, opt-in. "flag" adds
    # an X-Guardrail-Flag header and lets the request through; "block" rejects
    # it with 403.
    #
    # "flag" is the default because these are regexes, and a regex that is wrong
    # in the blocking direction deletes a legitimate request with no recourse,
    # while one that is wrong in the flagging direction costs a header. Pattern
    # matching cannot be a security boundary against an adversary who can
    # rephrase (arXiv:2504.11168), so its realistic value is *signal* — and
    # signal doesn't require blocking. Turn on "block" when you have measured
    # the false-positive rate against your own traffic.
    guardrails_enabled: bool = False
    guardrails_action: Literal["block", "flag"] = "flag"

    # Output redaction (OWASP LLM02). Heuristic, opt-in: scrubs common secret/
    # API-key patterns from the assistant's `content` before it reaches the
    # client and before caching/idempotency storage. Covers streaming too, via
    # guardrails.StreamRedactor — see docs/architecture.md.
    output_redaction_enabled: bool = False

    log_format: Literal["text", "json"] = "text"

    # Routing
    default_provider: str = "echo"

    # Restrict which providers a *request* may reach, comma-separated (e.g.
    # "openai,echo"). Empty (default) = every registered provider, today's
    # behavior. This governs everything a client can steer: an explicit
    # `provider`, the provider a model name routes to, and request-level
    # `fallbacks`. default_provider is always allowed (it's the operator's own
    # choice, so an allowlist can't accidentally lock the gateway out of its
    # own default). Without this an operator who configures server-side keys
    # for several providers has no way to say "tenants may only spend on this
    # one" — every authenticated caller can name any of them.
    allowed_providers: str = ""

    # Fallback: ordered "provider:model" targets tried on upstream (5xx) errors.
    # e.g. "openai:gpt-4o-mini,echo" — model is optional (defaults to request model).
    fallback_enabled: bool = False
    fallback_targets: str = ""

    # Cache
    cache_enabled: bool = True
    cache_ttl_seconds: int = Field(default=3600, ge=0)
    redis_url: str | None = None  # e.g. redis://localhost:6379/0

    # Metrics persistence (only active when redis_url is set)
    metrics_persist_interval_seconds: int = Field(default=30, ge=1)
    # Identifies this replica when persisting metrics, so multiple replicas each
    # write their own snapshot key (aggregated at read time) instead of
    # overwriting a single shared key. Defaults to a random per-process id.
    instance_id: str | None = None

    # Rate limiting (token bucket per client)
    rate_limit_enabled: bool = True
    rate_limit_requests: int = Field(default=60, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1)

    # Reject /v1/* request bodies larger than this many bytes (0 disables).
    max_body_bytes: int = Field(default=1_000_000, ge=0)

    # Cap on /v1/* requests in flight at once; excess get 429 + Retry-After
    # rather than queueing. 0 (default) = unlimited, today's behavior.
    #
    # The rate limiter bounds *arrivals*, which is a different quantity: 60
    # requests/min is satisfiable by 60 concurrent 60-second streams. And
    # httpx's read timeout resets per chunk, so a slow-trickling upstream can
    # hold a streaming request open past request_timeout_seconds indefinitely.
    # Process-local, like the default rate limiter — with N uvicorn workers the
    # effective cap is N × this.
    max_concurrent_requests: int = Field(default=0, ge=0)

    # Per-client spend cap (opt-in). Once a client's cumulative cost_usd_total
    # (tracked in usage_by_client) reaches this, further /v1/* requests from that
    # client get 402 until an operator resets metrics. Unset = no cap.
    client_budget_usd: float | None = Field(default=None, ge=0.0)

    # Per-key overrides for the above, e.g. "sk-a:5.00,sk-b:20.00". A key not
    # listed here falls back to client_budget_usd. Only meaningful when that
    # key also appears in api_keys (there's no per-IP override).
    client_budgets_usd: str = ""

    # Time-box the cap above to a fixed calendar window instead of lifetime
    # spend, e.g. 86400 for "$X per day" or 2_592_000 for "$X per 30 days".
    # Unset (default) = lifetime-cumulative, today's behavior. Windows are
    # fixed (epoch-aligned via int(now / window), the same idiom
    # RedisRateLimiter uses for its windows), not rolling from a client's
    # first request. Tracked separately from usage_by_client (which stays
    # lifetime for /v1/usage and /metrics observability) so a window rollover
    # clears enforcement without erasing historical totals — and, since it's
    # kept outside the persisted metrics snapshot, a restart resets the
    # current window's accumulated spend (see docs/architecture.md).
    client_budget_window_seconds: int | None = Field(default=None, ge=1)

    # Cap on distinct clients tracked in usage_by_client and the budget-window
    # store (0 = unlimited). Without auth the client id is the raw request IP,
    # so an internet-facing deployment would otherwise accumulate one entry per
    # IP forever — the same unbounded-growth risk the rate limiter already
    # guards against with its own bucket cap. When full, the entry with the
    # fewest requests is evicted to make room (protecting active tenants);
    # eviction resets that client's lifetime-budget baseline, consistent with
    # budget enforcement being approximate, not billing (docs/architecture.md).
    max_tracked_clients: int = Field(default=10_000, ge=0)

    # /metrics is open by default (so Prometheus can scrape without a token),
    # even when api_keys gates /v1/*. It carries a per-client cost/token
    # breakdown (usage_by_client), so an operator who considers that sensitive
    # can require the same Bearer key here too. No-op when api_keys is empty.
    metrics_require_auth: bool = False

    # Override or extend rekai/pricing.py's built-in table without a code
    # change or redeploy, e.g. "gpt-4o:2.00:8.00,my-model:0.50:1.50" (USD per
    # 1M tokens, input:output). An entry for an existing prefix replaces it;
    # a new prefix prices an otherwise-unknown model. Every cost estimate,
    # budget cap, and /v1/models pricing field is driven by this table, so
    # this is how an operator keeps it current between RekAI releases.
    pricing_overrides: str = ""

    # Retry transient (5xx/timeout) upstream failures with exponential backoff +
    # jitter before falling over. attempts is the total tries per target (1 = off).
    retry_max_attempts: int = Field(default=2, ge=1)
    retry_base_delay_seconds: float = Field(default=0.5, ge=0.0)
    retry_max_delay_seconds: float = Field(default=8.0, ge=0.0)

    # After a provider 429s, skip it for a while (its Retry-After, else this
    # default) so requests route to a healthy fallback instead of hammering it.
    provider_cooldown_enabled: bool = True
    provider_cooldown_seconds: float = Field(default=30.0, ge=0.0)

    # A 429 parks a provider immediately (see above); a 5xx needs this many
    # consecutive failures (across separate requests, resets on any success)
    # before it's parked the same way — a lightweight circuit breaker so a
    # persistently failing provider stops being retried on every request.
    circuit_breaker_threshold: int = Field(default=3, ge=1)

    # Idempotency-Key: replay the stored response for a repeated key for this long
    # (needs the cache backend; a no-op when caching is disabled).
    idempotency_ttl_seconds: int = Field(default=86_400, ge=0)

    # Semantic cache: reuse a response when a prior prompt's embedding is within
    # the cosine threshold. Opt-in (one embedding call per request). Process-local,
    # scoped per client, and TTL'd by cache_ttl_seconds.
    #
    # semantic_cache_model has no default on purpose: a hit here answers a prompt
    # that was never sent, so the threshold means nothing unless the embedding is
    # genuinely semantic. Enabling the cache without naming a model is refused at
    # startup rather than quietly falling back to `echo`, whose 16-dimension hash
    # "embedding" puts every vector in the positive orthant — unrelated prompts
    # sit around 0.78 cosine and ~12% of pairs clear the 0.85 default.
    semantic_cache_enabled: bool = False
    semantic_cache_model: str = ""
    semantic_cache_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    semantic_cache_max_entries: int = Field(default=1000, ge=1)

    # Provider defaults (server-side keys; BYOK via header overrides these)
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    ollama_base_url: str = "http://localhost:11434"
    anthropic_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    anthropic_version: str = "2023-06-01"
    anthropic_default_max_tokens: int = 1024
    gemini_api_key: str | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"

    # A custom OpenAI-compatible backend (Groq, Together, OpenRouter, Mistral,
    # local vLLM/LM Studio, …). Enabled when a base URL is configured.
    custom_name: str = "custom"
    custom_base_url: str | None = None
    custom_api_key: str | None = None
    custom_models: str = ""  # comma-separated, for /v1/models listing
    custom_embedding_models: str = ""  # comma-separated embedding models

    # Networking
    request_timeout_seconds: float = 60.0

    # Total wall-clock budget for one /v1/chat or /v1/embeddings request,
    # across *every* retry and fallback attempt. 0 (default) = unlimited,
    # today's behavior.
    #
    # request_timeout_seconds above bounds a single upstream call. It reads
    # like a request bound and is not one: attempts multiply by
    # retry_max_attempts and again by the length of the fallback chain, so the
    # shipped defaults (60s, 2 attempts, a 3-target chain) let one client wait
    # ~384s while holding a connection and a concurrency slot. This is the
    # distinction Envoy draws between a route timeout and a per-try timeout,
    # and that LiteLLM/Portkey expose as separate settings; RekAI only had the
    # per-try half. Streaming is deliberately exempt — a stream's duration is
    # the length of the answer, not a fault (REKAI_MAX_CONCURRENT_REQUESTS
    # bounds occupancy there).
    request_deadline_seconds: float = Field(default=0.0, ge=0.0)
    cors_origins: str = "*"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]

    @property
    def gateway_auth_enabled(self) -> bool:
        """Whether ``/v1/*`` requires an ``Authorization: Bearer`` key.

        Mirrors the middleware's own condition exactly: a static key list, or the
        dynamic keystore being enabled (which can hold runtime-added keys).
        """
        return bool(self.api_key_list) or self.dynamic_keys_enabled

    @property
    def server_provider_key_names(self) -> list[str]:
        """The env-var names of provider keys configured server-side.

        These are spent on the *operator's* account, unlike a BYOK key that the
        caller supplies per request and that RekAI never stores.
        """
        configured = {
            "REKAI_OPENAI_API_KEY": self.openai_api_key,
            "REKAI_ANTHROPIC_API_KEY": self.anthropic_api_key,
            "REKAI_GEMINI_API_KEY": self.gemini_api_key,
            "REKAI_CUSTOM_API_KEY": self.custom_api_key,
        }
        return sorted(name for name, value in configured.items() if value)

    def open_proxy_hazard(self) -> str | None:
        """The one configuration that is unsafe rather than merely permissive.

        A gateway with **no client auth** and a **server-side provider key** is
        an open, unauthenticated proxy to a paid API: anyone who can reach the
        port spends the operator's money, and the request looks legitimate to the
        provider. Neither half is a problem alone — an open gateway with no
        server key can only serve BYOK and `echo`, and a server key behind auth
        is the ordinary single-tenant deployment.

        Returns a message naming both the hazard and the fixes, or None.
        """
        if self.gateway_auth_enabled:
            return None
        names = self.server_provider_key_names
        if not names:
            return None
        return (
            f"Server-side provider keys are set ({', '.join(names)}) but gateway "
            "auth is not: /v1/* is reachable without a key, so anyone who can "
            "reach this port can spend them. Set REKAI_API_KEYS=<key>[,<key>...] "
            "(or REKAI_DYNAMIC_KEYS_ENABLED=true), or drop the server-side keys "
            "and let callers bring their own via the X-Provider-Key header."
        )

    @property
    def allowed_provider_list(self) -> list[str]:
        """Providers a request may reach, or ``[]`` meaning "no restriction"."""
        return [p.strip() for p in self.allowed_providers.split(",") if p.strip()]

    @property
    def client_budget_overrides(self) -> dict[str, float]:
        """Parse ``client_budgets_usd`` into ``{raw_key: usd_cap}``, keyed by the
        raw API key (not the masked client id) since that's how operators set it.
        Malformed entries (no ``:``, non-numeric amount) are skipped."""
        overrides: dict[str, float] = {}
        for raw in self.client_budgets_usd.split(","):
            raw = raw.strip()
            if not raw or ":" not in raw:
                continue
            key, _, amount = raw.partition(":")
            key = key.strip()
            if not key:
                continue
            try:
                overrides[key] = float(amount.strip())
            except ValueError:
                continue
        return overrides

    @property
    def pricing_override_dict(self) -> dict[str, tuple[float, float]]:
        """Parse ``pricing_overrides`` into ``{model_prefix: (input, output)}``
        USD-per-1M pairs, for :func:`rekai.pricing.price_for_model`. Entries
        missing the ``prefix:input:output`` shape, or with a non-numeric price,
        are skipped rather than raising — a typo shouldn't take the gateway down."""
        overrides: dict[str, tuple[float, float]] = {}
        for raw in self.pricing_overrides.split(","):
            raw = raw.strip()
            if not raw:
                continue
            parts = raw.split(":")
            if len(parts) != 3:
                continue
            prefix, input_str, output_str = (p.strip() for p in parts)
            if not prefix:
                continue
            try:
                overrides[prefix.lower()] = (float(input_str), float(output_str))
            except ValueError:
                continue
        return overrides

    @property
    def custom_model_list(self) -> list[str]:
        return [m.strip() for m in self.custom_models.split(",") if m.strip()]

    @property
    def custom_embedding_model_list(self) -> list[str]:
        return [m.strip() for m in self.custom_embedding_models.split(",") if m.strip()]

    @property
    def fallback_target_list(self) -> list[tuple[str, str | None]]:
        """Parse ``fallback_targets`` into ordered ``(provider, model | None)`` tuples."""
        targets: list[tuple[str, str | None]] = []
        for raw in self.fallback_targets.split(","):
            raw = raw.strip()
            if not raw:
                continue
            provider, _, model = raw.partition(":")
            provider = provider.strip()
            if provider:
                targets.append((provider, model.strip() or None))
        return targets


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
