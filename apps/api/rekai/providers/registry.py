"""Provider registry — the single place that knows every available provider."""

from __future__ import annotations

from rekai.config import get_settings
from rekai.providers.anthropic import AnthropicProvider
from rekai.providers.base import Provider
from rekai.providers.echo import EchoProvider
from rekai.providers.gemini import GeminiProvider
from rekai.providers.ollama import OllamaProvider
from rekai.providers.openai import OpenAIProvider
from rekai.providers.openai_compatible import OpenAICompatibleProvider

_PROVIDERS: dict[str, Provider] = {
    p.name: p
    for p in (
        EchoProvider(),
        OpenAIProvider(),
        AnthropicProvider(),
        GeminiProvider(),
        OllamaProvider(),
    )
}

# Register a custom OpenAI-compatible backend when configured.
_settings = get_settings()
if _settings.custom_base_url:
    _custom = OpenAICompatibleProvider(
        name=_settings.custom_name,
        base_url=_settings.custom_base_url,
        api_key=_settings.custom_api_key,
        models=_settings.custom_model_list,
    )
    _PROVIDERS[_custom.name] = _custom


def get_provider(name: str) -> Provider | None:
    return _PROVIDERS.get(name)


def provider_names() -> list[str]:
    return sorted(_PROVIDERS)


def register_provider(provider: Provider) -> None:
    """Register (or replace) a provider at runtime — useful for plugins/tests."""
    _PROVIDERS[provider.name] = provider
