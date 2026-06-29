"""Routing — decide which provider should handle a request.

Resolution order:

1. An explicit ``provider`` on the request.
2. A known model-name prefix (e.g. ``gpt-*`` → openai, ``llama*`` → ollama).
3. The configured default provider.
"""

from __future__ import annotations

from rekai.config import Settings
from rekai.providers import Provider, get_provider
from rekai.providers.base import ProviderError
from rekai.schemas import ChatRequest

# Ordered (prefix, provider) rules. First match wins.
_MODEL_PREFIX_RULES: tuple[tuple[str, str], ...] = (
    ("gpt-", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("claude", "anthropic"),
    ("gemini", "gemini"),
    ("text-embedding", "openai"),
    ("llama", "ollama"),
    ("mistral", "ollama"),
    ("qwen", "ollama"),
    ("gemma", "ollama"),
    ("phi", "ollama"),
    ("echo", "echo"),
)


def resolve_provider(provider: str | None, model: str, settings: Settings) -> str:
    """Resolve a provider name from an explicit choice, model prefix, or default."""
    if provider:
        return provider
    model_lower = model.lower()
    for prefix, name in _MODEL_PREFIX_RULES:
        if model_lower.startswith(prefix):
            return name
    return settings.default_provider


def resolve_provider_name(request: ChatRequest, settings: Settings) -> str:
    return resolve_provider(request.provider, request.model, settings)


def select_provider(request: ChatRequest, settings: Settings) -> tuple[str, Provider]:
    name = resolve_provider_name(request, settings)
    provider = get_provider(name)
    if provider is None:
        raise ProviderError(f"Unknown provider '{name}'.", status_code=400)
    return name, provider
