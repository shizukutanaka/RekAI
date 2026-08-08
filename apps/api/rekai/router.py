"""Routing — decide which provider should handle a request.

Resolution order:

1. An explicit ``provider`` on the request.
2. A known model-name prefix (e.g. ``gpt-*`` → openai, ``llama*`` → ollama).
3. The configured default provider.
"""

from __future__ import annotations

from rekai.config import Settings
from rekai.models import PROVIDER_PREFIXES, provider_for_prefix
from rekai.providers import Provider, get_provider
from rekai.providers.base import ProviderError
from rekai.schemas import ChatRequest

# Ordered (prefix, provider) rules, from the single model registry
# (rekai/models.py) so routing can't drift from pricing / the model list.
_MODEL_PREFIX_RULES: tuple[tuple[str, str], ...] = PROVIDER_PREFIXES


def resolve_provider(provider: str | None, model: str, settings: Settings) -> str:
    """Resolve a provider name from an explicit choice, model prefix, or default."""
    if provider:
        return provider
    return provider_for_prefix(model) or settings.default_provider


def resolve_provider_name(request: ChatRequest, settings: Settings) -> str:
    return resolve_provider(request.provider, request.model, settings)


def ensure_allowed(name: str, settings: Settings) -> None:
    """Reject a provider the operator hasn't enabled for requests (403).

    Everything a client can steer — an explicit ``provider``, the provider a
    model name routes to, and request-level ``fallbacks`` — funnels through
    this. ``REKAI_ALLOWED_PROVIDERS`` empty means no restriction, and
    ``default_provider`` is always allowed so an allowlist can't lock the
    gateway out of its own default.
    """
    allowed = settings.allowed_provider_list
    if not allowed or name == settings.default_provider or name in allowed:
        return
    raise ProviderError(
        f"Provider '{name}' is not enabled on this gateway.",
        status_code=403,
    )


def select_provider(request: ChatRequest, settings: Settings) -> tuple[str, Provider]:
    name = resolve_provider_name(request, settings)
    ensure_allowed(name, settings)
    provider = get_provider(name)
    if provider is None:
        raise ProviderError(f"Unknown provider '{name}'.", status_code=400)
    return name, provider
