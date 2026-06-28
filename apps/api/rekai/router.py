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
    ("llama", "ollama"),
    ("mistral", "ollama"),
    ("qwen", "ollama"),
    ("gemma", "ollama"),
    ("phi", "ollama"),
    ("echo", "echo"),
)


def resolve_provider_name(request: ChatRequest, settings: Settings) -> str:
    if request.provider:
        return request.provider
    model = request.model.lower()
    for prefix, provider in _MODEL_PREFIX_RULES:
        if model.startswith(prefix):
            return provider
    return settings.default_provider


def select_provider(request: ChatRequest, settings: Settings) -> tuple[str, Provider]:
    name = resolve_provider_name(request, settings)
    provider = get_provider(name)
    if provider is None:
        raise ProviderError(f"Unknown provider '{name}'.", status_code=400)
    return name, provider
