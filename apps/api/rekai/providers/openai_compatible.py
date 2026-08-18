"""A configurable provider for any OpenAI-compatible chat API.

Many providers speak the OpenAI ``/chat/completions`` API — Groq, Together,
OpenRouter, Mistral, Fireworks, and local servers like vLLM or LM Studio. This
reuses :class:`OpenAIProvider` end to end (including accurate streaming usage),
just pointed at a different base URL and key.
"""

from __future__ import annotations

from rekai.providers.openai import OpenAIProvider


class OpenAICompatibleProvider(OpenAIProvider):
    # Not every OpenAI-compatible backend has a credential. vLLM, LM Studio and
    # llama.cpp — three of the ones this provider exists to reach — serve
    # unauthenticated by default, and requiring a key locked RekAI out of them
    # entirely: pointing REKAI_CUSTOM_BASE_URL at a local server and asking for a
    # completion returned RekAI's own 401 ("No custom API key...") without a
    # request ever leaving the process. A key is sent when one is configured (or
    # supplied per request via BYOK) and omitted when there is none, so a hosted
    # backend that does need one answers with its own 401 instead.
    requires_key = False

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None = None,
        models: list[str] | None = None,
        embedding_models: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.name = name
        self._url = base_url
        self._key = api_key
        self._models = models or []
        self._embedding_models = embedding_models or []

    def _base_url(self) -> str:
        return self._url

    def _server_key(self) -> str | None:
        return self._key

    def _key_env_hint(self) -> str:
        return "REKAI_CUSTOM_API_KEY"

    async def list_models(self, api_key: str | None) -> list[str]:
        return list(self._models)

    async def list_embedding_models(self, api_key: str | None) -> list[str]:
        # Custom backends don't inherit OpenAI's embedding model names; they
        # advertise their own via REKAI_CUSTOM_EMBEDDING_MODELS.
        return list(self._embedding_models)
