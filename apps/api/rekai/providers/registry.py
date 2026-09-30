"""Provider registry — the single place that knows every available provider.

Built-ins are registered lazily on first access rather than at import time, so
importing the registry is side-effect free, and the settings-dependent custom
backend is registered by ``create_app`` (via :func:`configure_custom_provider`)
instead of reading the process env at import — that is what lets the running
app's ``Settings`` reach the provider layer (O-1).
"""

from __future__ import annotations

from rekai.config import Settings
from rekai.providers.anthropic import AnthropicProvider
from rekai.providers.base import Provider
from rekai.providers.echo import EchoProvider
from rekai.providers.gemini import GeminiProvider
from rekai.providers.ollama import OllamaProvider
from rekai.providers.openai import OpenAIProvider
from rekai.providers.openai_compatible import OpenAICompatibleProvider

_PROVIDERS: dict[str, Provider] = {}
_BUILTINS: dict[str, type[Provider]] = {
    cls.name: cls
    for cls in (EchoProvider, OpenAIProvider, AnthropicProvider, GeminiProvider, OllamaProvider)
}
_builtins_registered = False
_custom_name: str | None = None


def _ensure_builtins() -> None:
    global _builtins_registered
    if _builtins_registered:
        return
    for name, cls in _BUILTINS.items():
        # setdefault: a runtime-registered provider of the same name wins.
        _PROVIDERS.setdefault(name, cls())
    _builtins_registered = True


def configure_custom_provider(settings: Settings) -> None:
    """Register (or remove) the custom OpenAI-compatible backend from the app's
    settings. Called by ``create_app`` — re-running it with different settings
    replaces the custom provider, and running it without ``custom_base_url``
    removes a stale one rather than leaking it into a later app."""
    global _custom_name
    _ensure_builtins()
    if _custom_name is not None:
        _PROVIDERS.pop(_custom_name, None)
        if _custom_name in _BUILTINS:
            # A custom backend shadowing a built-in name restores the built-in
            # rather than leaving the name empty.
            _PROVIDERS[_custom_name] = _BUILTINS[_custom_name]()
        _custom_name = None
    if settings.custom_base_url:
        custom = OpenAICompatibleProvider(
            name=settings.custom_name,
            base_url=settings.custom_base_url,
            api_key=settings.custom_api_key,
            models=settings.custom_model_list,
            embedding_models=settings.custom_embedding_model_list,
        )
        _PROVIDERS[custom.name] = custom
        _custom_name = custom.name


def get_provider(name: str) -> Provider | None:
    _ensure_builtins()
    return _PROVIDERS.get(name)


def provider_names() -> list[str]:
    _ensure_builtins()
    return sorted(_PROVIDERS)


def register_provider(provider: Provider) -> None:
    """Register (or replace) a provider at runtime — useful for plugins/tests."""
    _ensure_builtins()
    _PROVIDERS[provider.name] = provider
