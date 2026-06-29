"""A configurable provider for any OpenAI-compatible chat API.

Many providers speak the OpenAI ``/chat/completions`` API — Groq, Together,
OpenRouter, Mistral, Fireworks, and local servers like vLLM or LM Studio. This
reuses :class:`OpenAIProvider` end to end (including accurate streaming usage),
just pointed at a different base URL and key.
"""

from __future__ import annotations

from rekai.providers.openai import OpenAIProvider


class OpenAICompatibleProvider(OpenAIProvider):
    requires_key = True

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None = None,
        models: list[str] | None = None,
    ) -> None:
        self.name = name
        self._url = base_url
        self._key = api_key
        self._models = models or []

    def _base_url(self) -> str:
        return self._url

    def _server_key(self) -> str | None:
        return self._key

    def _key_env_hint(self) -> str:
        return "REKAI_CUSTOM_API_KEY"

    async def list_models(self, api_key: str | None) -> list[str]:
        return list(self._models)
