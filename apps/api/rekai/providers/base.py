"""Provider abstraction.

A Provider knows how to turn a :class:`ChatRequest` into a chat completion by
talking to a specific backend (OpenAI, Ollama, …). Adding a new provider means
implementing this small interface and registering it.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Literal

import httpx

from rekai.schemas import ChatRequest, Usage
from rekai.tracing import current_traceparent, current_tracestate


def trace_headers() -> dict[str, str]:
    """W3C propagation headers for the outbound call a provider is about to
    make, or ``{}`` outside a request context — so distributed tracing doesn't
    stop at RekAI's edge. Merge into a provider's request headers, e.g.
    ``{**trace_headers(), "Authorization": ...}``.

    ``tracestate`` rides along with ``traceparent``: the spec pairs them, and
    forwarding one without the other strands whatever vendor state the caller
    put there. A gateway is the hop where that matters most, since every call
    crosses it."""
    traceparent = current_traceparent()
    if not traceparent:
        return {}
    headers = {"traceparent": traceparent}
    tracestate = current_tracestate()
    if tracestate:
        headers["tracestate"] = tracestate
    return headers


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


# HTTP header values must be printable ASCII (optionally HTAB/obs-text on
# the wire, but httpx encodes them as ASCII and httpcore rejects control
# bytes at write time). Client-controlled strings spliced into upstream
# headers — BYOK keys, forwarded compat headers, Anthropic's profile-id —
# can carry characters outside this range (obs-text via an inbound header,
# anything via a JSON body field) and must be rejected up front: a non-ASCII
# value raises UnicodeEncodeError at request build (an unhandled 500), and
# \x00-\x1f/\x7f fails mid-call as a LocalProtocolError.
_HEADER_VALUE = re.compile(r"[\x20-\x7e]*")


def check_header_safe(name: str, value: str) -> None:
    """Reject a client-controlled string that can't ride in an upstream header.

    ``name`` identifies the source (a header or field name), never the value —
    the value may be a credential."""
    if not _HEADER_VALUE.fullmatch(value):
        raise ProviderError(
            f"invalid character in {name}: must be printable ASCII to send as an upstream header",
            status_code=400,
        )


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


# Why a provider must report *why* it stopped, normalized to OpenAI's vocabulary:
#
#   stop            the model finished on its own
#   length          it was cut off by max_tokens — the answer is INCOMPLETE
#   tool_calls      it stopped to call a tool
#   content_filter  the provider's safety layer stopped it
#
# Every backend reports this natively (OpenAI `finish_reason`, Anthropic
# `stop_reason`, Gemini `finishReason`, Ollama `done_reason`) and RekAI used to
# discard all of them, synthesising "stop" at the edge. That turned a truncated
# answer into one indistinguishable from a complete one, which breaks the
# standard "retry with a larger budget when finish_reason == 'length'" pattern
# and, worse, gets cached and replayed as if it were whole.
#
# ``None`` means the provider said nothing — the honest answer for a backend
# that doesn't report it, and what keeps responses cached before this existed
# readable.
FinishReason = Literal["stop", "length", "tool_calls", "content_filter"]


@dataclass
class ProviderResult:
    content: str
    model: str
    usage: Usage = field(default_factory=Usage)
    tool_calls: list[dict] | None = None
    finish_reason: FinishReason | None = None
    # OpenAI's response-side identifiers: which backend config served the call
    # (used with `seed` for determinism debugging) and which service tier
    # actually handled it when the request said "auto". None elsewhere.
    system_fingerprint: str | None = None
    service_tier: str | None = None
    # Anthropic reports *which* stop sequence ended the turn (`stop_sequence`
    # alongside `stop_reason: "stop_sequence"`). None for providers that don't
    # say — OpenAI's API has no equivalent field.
    stop_sequence: str | None = None
    # The model's refusal text (OpenAI `message.refusal`) — kept out of
    # ``content``, which stays "" on a refusal so "no answer" stays honest.
    refusal: str | None = None
    # Web-search citations etc. (OpenAI `message.annotations`) — raw dicts,
    # passed through verbatim so the caller sees what the model cited.
    annotations: list[dict] | None = None
    # Anthropic extended-thinking blocks (thinking/redacted_thinking), verbatim
    # — text and signature the caller must echo back on the next turn.
    thinking_blocks: list[dict] | None = None
    # Anthropic citations on the answer's text (web-search sources), verbatim.
    # Each carries its own cited_text, so a flat list stays faithful even over
    # the provider's flat-text model.
    citations: list[dict] | None = None
    # Anthropic server-side tool blocks (server_tool_use, web_search_tool_result,
    # mcp_tool_use/result, code_execution_tool_result, ...) and any block type
    # the provider doesn't map — verbatim, in upstream order. Keeping unknown
    # types verbatim is forward-compatible: new Anthropic blocks pass through
    # untouched instead of vanishing.
    extra_blocks: list[dict] | None = None
    # The upstream content array verbatim — the ordered sequence the provider
    # actually emitted (text, thinking, tool_use, and extra blocks interleaved
    # in upstream order). Flattening loses that order, so the verbatim copy
    # backs the /v1/messages surface and history echo.
    content_blocks: list[dict] | None = None
    # Message-level fields the provider doesn't map (container for code
    # execution, context_management edit reports, ...) — verbatim, so new
    # upstream fields surface instead of vanishing.
    extra_fields: dict | None = None


@dataclass
class EmbeddingResult:
    embeddings: list[list[float]]
    model: str
    usage: Usage = field(default_factory=Usage)


@dataclass
class ModerationResult:
    id: str | None
    model: str
    # Verbatim upstream result entries (flagged, categories, scores) — the
    # category sets differ between moderation model versions.
    results: list[dict]


@dataclass
class StreamEvent:
    """One event from a streaming completion: a text ``delta``, and/or (yielded
    once at the end when available) provider-reported ``usage``, assembled
    ``tool_calls``, and the normalized ``finish_reason``."""

    delta: str | None = None
    usage: Usage | None = None
    tool_calls: list[dict] | None = None
    finish_reason: FinishReason | None = None
    # OpenAI puts these on every chunk (including the role announcement, which
    # carries no delta) — see ProviderResult for what they mean.
    system_fingerprint: str | None = None
    service_tier: str | None = None
    stop_sequence: str | None = None
    # OpenAI streams refusal text as `delta.refusal` chunks, separate from
    # `delta.content` — kept apart for the same reason as ProviderResult.
    refusal_delta: str | None = None
    # OpenAI streams annotations complete inside one delta chunk.
    annotations: list[dict] | None = None
    # Anthropic extended-thinking stream pieces: a thinking_delta text chunk,
    # the block's closing signature, or a whole redacted_thinking block.
    thinking_delta: str | None = None
    thinking_signature: str | None = None
    thinking_block: dict | None = None
    # A web-search citation arriving inside a text block (citations_delta).
    citation: dict | None = None
    # A non-standard content block streaming through verbatim: the upstream
    # content_block_start payload, one verbatim delta, or the completed block
    # at content_block_stop.
    extra_block_start: dict | None = None
    extra_block_delta: dict | None = None
    extra_block: dict | None = None
    # Message-level fields arriving on message_start that the provider
    # doesn't map (container, context_management, ...), verbatim.
    extra_fields: dict | None = None


class Provider(ABC):
    """Base class for all providers."""

    #: Unique provider identifier, e.g. ``"openai"``.
    name: str

    #: Whether this provider requires an API key (server-side or BYOK).
    requires_key: bool = True

    #: Largest ``dimensions`` value this provider will embed — enforced before
    #: the embeddings cache lookup and idempotent replay, since both serve
    #: stored responses without calling the provider. ``None`` means the
    #: request value is forwarded verbatim (the contract for real providers).
    max_embedding_dimensions: int | None = None

    def __init__(self) -> None:
        self._http_client: httpx.AsyncClient | None = None
        self._http_client_loop: asyncio.AbstractEventLoop | None = None
        self._http_client_timeout: float | None = None

    def _client(self, timeout: float) -> httpx.AsyncClient:
        """A persistent ``httpx.AsyncClient`` reused across requests.

        Providers are long-lived singletons (built once by
        ``providers/registry.py``), but every call used to open a brand-new
        ``AsyncClient`` in an ``async with`` block and tear it down again —
        so no TCP/TLS connection to an upstream provider was ever reused,
        paying a full handshake on every single chat/embeddings call.

        An ``AsyncClient``'s connection pool is bound to the event loop it was
        created on, so this recreates the client whenever the running loop
        differs from the one it was last built on (harmless in production,
        which has exactly one loop for the process lifetime — the persistent-
        reuse path is what actually matters there; it only fires routinely
        under pytest-asyncio, where each test function gets its own loop —
        which conveniently also means a test that monkeypatches
        ``httpx.AsyncClient`` never observes a client cached from a prior
        test). The old client, if any, is intentionally not awaited-closed
        here: its loop may already be closed, and letting it fall out of
        scope is a standard tradeoff for this pattern. (Not keyed on
        ``.is_closed`` — nothing in this codebase ever explicitly closes a
        cached client, and test doubles for ``httpx.AsyncClient`` don't
        implement that attribute.)

        The client is also rebuilt when the requested ``timeout`` differs from
        the one it was built with — so re-running ``create_app`` with a changed
        ``request_timeout_seconds`` takes effect on the next call instead of
        being frozen at the value seen when the client was first constructed.
        """
        loop = asyncio.get_running_loop()
        if (
            self._http_client is None
            or self._http_client_loop is not loop
            or self._http_client_timeout != timeout
        ):
            # A float `timeout=` would set every phase to the same value, so a
            # black-holed host would hold the attempt for the full read budget
            # before the retry/fallback loop could move on. The connect phase
            # keeps httpx's fail-fast default (capped by the request timeout):
            # a dead host fails in seconds, freeing the rest of the budget.
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=min(5.0, timeout))
            )
            self._http_client_loop = loop
            self._http_client_timeout = timeout
        return self._http_client

    async def aclose(self) -> None:
        """Close the persistent client, if one was built.

        Providers are process-lifetime singletons, but their connection pools
        should drain politely on app shutdown rather than be severed when the
        loop ends. Tolerant by construction: a client whose loop already ended
        (or a test double without ``aclose``) is dropped without failing the
        shutdown path.
        """
        # Subclasses that skip ``super().__init__()`` (test doubles, minimal
        # stubs) may not have the attribute at all — teardown must not trip on it.
        client = getattr(self, "_http_client", None)
        self._http_client = None
        self._http_client_loop = None
        self._http_client_timeout = None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

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

    async def embed(
        self,
        inputs: list[str],
        model: str,
        api_key: str | None,
        *,
        dimensions: int | None = None,
        encoding_format: str | None = None,
        user: str | None = None,
    ) -> EmbeddingResult:
        """Embed one or more texts. Providers that support embeddings override this.

        ``dimensions``/``encoding_format`` are OpenAI's request fields —
        providers without them ignore them (and a provider that understands a
        different name maps it, e.g. Gemini's ``outputDimensionality``).
        ``user`` is OpenAI's end-user id for abuse detection — ignored by
        providers with no such field."""
        raise ProviderError(f"{self.name} does not support embeddings.", status_code=400)

    async def moderate(
        self,
        input: str | list[str] | list[dict],
        model: str,
        api_key: str | None,
    ) -> ModerationResult:
        """Classify input against safety categories (OpenAI ``/v1/moderations``).

        ``input`` is OpenAI's shape verbatim — a string, a list of strings, or
        a content-part list (``text``/``image_url``). Providers without a
        moderation endpoint keep the default 400."""
        raise ProviderError(f"{self.name} does not support moderation.", status_code=400)

    async def list_models(self, api_key: str | None) -> list[str]:
        """Return known model ids. Override when the backend can enumerate them."""
        return []

    async def list_embedding_models(self, api_key: str | None) -> list[str]:
        """Return known embedding model ids. Override when embeddings are supported."""
        return []
