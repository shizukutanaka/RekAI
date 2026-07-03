"""Provider abstraction.

A Provider knows how to turn a :class:`ChatRequest` into a chat completion by
talking to a specific backend (OpenAI, Ollama, …). Adding a new provider means
implementing this small interface and registering it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field

from rekai.schemas import ChatRequest, Usage
from rekai.tracing import current_traceparent


def trace_headers() -> dict[str, str]:
    """``{"traceparent": ...}`` for the outbound HTTP call a provider is about
    to make, or ``{}`` outside a request context — so distributed tracing
    doesn't stop at RekAI's edge. Merge into a provider's request headers,
    e.g. ``{**trace_headers(), "Authorization": ...}``."""
    traceparent = current_traceparent()
    return {"traceparent": traceparent} if traceparent else {}


class ProviderError(Exception):
    """Raised when a provider cannot fulfil a request.

    ``status_code`` is surfaced to the HTTP layer so client errors (e.g. a
    missing BYOK key) are not reported as 500s. ``retry_after`` carries the
    upstream ``Retry-After`` value (seconds) on a 429 so the gateway can wait the
    requested time and pass it on to the client.
    """

    def __init__(
        self, message: str, status_code: int = 502, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """Read a ``Retry-After`` header as whole seconds, if present and numeric.

    Only the delta-seconds form is supported (the form OpenAI/Anthropic/Gemini
    use); an HTTP-date value returns ``None``.
    """
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def provider_http_error(
    name: str, status_code: int, body: str, headers: Mapping[str, str] | None = None
) -> ProviderError:
    """Build a ProviderError from an upstream HTTP error response.

    5xx are normalised to 502 (bad gateway); a 429 captures ``Retry-After``.
    """
    retry_after = parse_retry_after(headers) if headers and status_code == 429 else None
    code = status_code if status_code < 500 else 502
    return ProviderError(
        f"{name} returned {status_code}: {body[:200]}",
        status_code=code,
        retry_after=retry_after,
    )


@dataclass
class ProviderResult:
    content: str
    model: str
    usage: Usage = field(default_factory=Usage)
    tool_calls: list[dict] | None = None


@dataclass
class EmbeddingResult:
    embeddings: list[list[float]]
    model: str
    usage: Usage = field(default_factory=Usage)


@dataclass
class StreamEvent:
    """One event from a streaming completion: a text ``delta``, and/or (yielded
    once at the end when available) provider-reported ``usage`` and assembled
    ``tool_calls``."""

    delta: str | None = None
    usage: Usage | None = None
    tool_calls: list[dict] | None = None


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

    async def stream_events(
        self, request: ChatRequest, api_key: str | None
    ) -> AsyncIterator[StreamEvent]:
        """Yield :class:`StreamEvent`s (text deltas, then optional usage).

        The default wraps :meth:`stream` and reports no usage, so the endpoint
        falls back to estimating tokens from the streamed text. Providers that
        can report exact usage override this.
        """
        async for delta in self.stream(request, api_key):
            yield StreamEvent(delta=delta)

    def server_key_configured(self) -> bool:
        """Whether a server-side key is configured for this provider.

        ``True`` means the provider is usable without a per-request BYOK key.
        Keyless providers are always ready; key-requiring providers override this
        to check their configured key.
        """
        return not self.requires_key

    async def embed(self, inputs: list[str], model: str, api_key: str | None) -> EmbeddingResult:
        """Embed one or more texts. Providers that support embeddings override this."""
        raise ProviderError(f"{self.name} does not support embeddings.", status_code=400)

    async def list_models(self, api_key: str | None) -> list[str]:
        """Return known model ids. Override when the backend can enumerate them."""
        return []

    async def list_embedding_models(self, api_key: str | None) -> list[str]:
        """Return known embedding model ids. Override when embeddings are supported."""
        return []
