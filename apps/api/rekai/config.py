"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings.

    All values can be overridden through environment variables (or a local
    ``.env`` file). See ``.env.example`` for the full list.
    """

    model_config = SettingsConfigDict(env_prefix="REKAI_", env_file=".env", extra="ignore")

    # General
    app_name: str = "RekAI"
    environment: str = "development"
    log_level: str = "INFO"

    # Gateway auth: comma-separated client API keys. When set, /v1/* requires
    # `Authorization: Bearer <key>`. Empty = open (no client auth).
    api_keys: str = ""
    log_format: str = "text"  # "text" (human-readable) or "json" (structured)

    # Routing
    default_provider: str = "echo"

    # Fallback: ordered "provider:model" targets tried on upstream (5xx) errors.
    # e.g. "openai:gpt-4o-mini,echo" — model is optional (defaults to request model).
    fallback_enabled: bool = False
    fallback_targets: str = ""

    # Cache
    cache_enabled: bool = True
    cache_ttl_seconds: int = 3600
    redis_url: str | None = None  # e.g. redis://localhost:6379/0

    # Metrics persistence (only active when redis_url is set)
    metrics_persist_interval_seconds: int = 30

    # Rate limiting (token bucket per client)
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60

    # Reject /v1/* request bodies larger than this many bytes (0 disables).
    max_body_bytes: int = 1_000_000

    # Retry transient (5xx/timeout) upstream failures with exponential backoff +
    # jitter before falling over. attempts is the total tries per target (1 = off).
    retry_max_attempts: int = 2
    retry_base_delay_seconds: float = 0.5
    retry_max_delay_seconds: float = 8.0

    # After a provider 429s, skip it for a while (its Retry-After, else this
    # default) so requests route to a healthy fallback instead of hammering it.
    provider_cooldown_enabled: bool = True
    provider_cooldown_seconds: float = 30.0

    # Idempotency-Key: replay the stored response for a repeated key for this long
    # (needs the cache backend; a no-op when caching is disabled).
    idempotency_ttl_seconds: int = 86_400

    # Semantic cache: reuse a response when a prior prompt's embedding is within
    # the cosine threshold. Opt-in (one embedding call per request); use a real
    # embeddings model. Process-local.
    semantic_cache_enabled: bool = False
    semantic_cache_model: str = "echo"
    semantic_cache_threshold: float = 0.85
    semantic_cache_max_entries: int = 1000

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
    cors_origins: str = "*"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]

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
