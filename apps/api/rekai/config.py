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

    # Routing
    default_provider: str = "echo"

    # Cache
    cache_enabled: bool = True
    cache_ttl_seconds: int = 3600
    redis_url: str | None = None  # e.g. redis://localhost:6379/0

    # Rate limiting (token bucket per client)
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60

    # Provider defaults (server-side keys; BYOK via header overrides these)
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    ollama_base_url: str = "http://localhost:11434"
    anthropic_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    anthropic_version: str = "2023-06-01"
    anthropic_default_max_tokens: int = 1024

    # Networking
    request_timeout_seconds: float = 60.0
    cors_origins: str = "*"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
