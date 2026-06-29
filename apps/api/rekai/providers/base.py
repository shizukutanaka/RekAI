"""Provider abstraction.

A Provider knows how to turn a :class:`ChatRequest` into a chat completion by
talking to a specific backend (OpenAI, Ollama, …). Adding a new provider means
implementing this small interface and registering it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from rekai.schemas import ChatRequest, Usage


class ProviderError(Exception):
    """Raised when a provider cannot fulfil a request.

    ``status_code`` is surfaced to the HTTP layer so client errors (e.g. a
    missing BYOK key) are not reported as 500s.
    """

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ProviderResult:
    content: str
    model: str
    usage: Usage = field(default_factory=Usage)


class Provider(ABC):
    """Base class for all providers."""

    #: Unique provider identifier, e.g. ``"openai"``.
    name: str

    #: Whether this provider requires an API key (server-side or BYOK).
    requires_key: bool = True

    @abstractmethod
    async def chat(self, request: ChatRequest, api_key: str | None) -> ProviderResult:
        """Execute a chat completion."""

    async def stream(self, request: ChatRequest, api_key: str | None) -> AsyncIterator[str]:
        """Yield response text incrementally.

        The default implementation falls back to a single :meth:`chat` call and
        yields the whole answer as one chunk, so every provider supports the
        streaming endpoint even without native streaming.
        """
        result = await self.chat(request, api_key)
        yield result.content

    def server_key_configured(self) -> bool:
        """Whether a server-side key is configured for this provider.

        ``True`` means the provider is usable without a per-request BYOK key.
        Keyless providers are always ready; key-requiring providers override this
        to check their configured key.
        """
        return not self.requires_key

    async def list_models(self, api_key: str | None) -> list[str]:
        """Return known model ids. Override when the backend can enumerate them."""
        return []
