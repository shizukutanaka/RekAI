"""Provider registry — the single place that knows every available provider."""

from __future__ import annotations

from rekai.providers.base import Provider
from rekai.providers.echo import EchoProvider
from rekai.providers.ollama import OllamaProvider
from rekai.providers.openai import OpenAIProvider

_PROVIDERS: dict[str, Provider] = {
    p.name: p
    for p in (
        EchoProvider(),
        OpenAIProvider(),
        OllamaProvider(),
    )
}


def get_provider(name: str) -> Provider | None:
    return _PROVIDERS.get(name)


def provider_names() -> list[str]:
    return sorted(_PROVIDERS)


def register_provider(provider: Provider) -> None:
    """Register (or replace) a provider at runtime — useful for plugins/tests."""
    _PROVIDERS[provider.name] = provider
