"""Synchronous and asynchronous clients for the RekAI gateway."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx

Message = dict[str, str]
Messages = str | list[Message]


class RekAIError(Exception):
    """Raised when the RekAI API returns an error response."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ChatResult:
    """A non-streamed chat response."""

    id: str
    provider: str
    model: str
    content: str
    usage: dict[str, int]
    cost_usd: float | None
    cached: bool
    fallback_used: bool
    tool_calls: list[dict[str, Any]] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatResult:
        return cls(
            id=data["id"],
            provider=data["provider"],
            model=data["model"],
            content=data["content"],
            usage=data.get("usage", {}),
            cost_usd=data.get("cost_usd"),
            cached=data.get("cached", False),
            fallback_used=data.get("fallback_used", False),
            tool_calls=data.get("tool_calls"),
        )


@dataclass
class EmbeddingsResult:
    """A text-embeddings response."""

    provider: str
    model: str
    embeddings: list[list[float]]
    usage: dict[str, int]
    cached: bool
    cost_usd: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EmbeddingsResult:
        return cls(
            provider=data["provider"],
            model=data["model"],
            embeddings=data.get("embeddings", []),
            usage=data.get("usage", {}),
            cached=data.get("cached", False),
            cost_usd=data.get("cost_usd"),
        )


def _normalize(messages: Messages) -> list[Message]:
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    return messages


# --- shared request/response plumbing (used by both sync and async clients) ---


def _build_headers(
    default_provider_key: str | None,
    default_gateway_key: str | None,
    provider_key: str | None,
    gateway_key: str | None,
) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = provider_key or default_provider_key
    if key:
        headers["X-Provider-Key"] = key
    # The gateway key authenticates this client to RekAI (REKAI_API_KEYS);
    # distinct from the provider key above, which is BYOK for the upstream
    # provider. Required on /v1/* whenever the deployment has keys configured.
    bearer = gateway_key or default_gateway_key
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return headers


def _build_payload(
    model: str,
    messages: Messages,
    provider: str | None,
    temperature: float,
    max_tokens: int | None,
    cache: bool,
    fallbacks: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any | None = None,
    response_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": _normalize(messages),
        "temperature": temperature,
        "cache": cache,
    }
    if provider is not None:
        payload["provider"] = provider
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if fallbacks is not None:
        payload["fallbacks"] = fallbacks
    if tools is not None:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    if response_format is not None:
        payload["response_format"] = response_format
    return payload


def _detail_from_body(status_code: int, body: Any) -> str:
    detail = f"RekAI returned {status_code}"
    if isinstance(body, dict):
        return body.get("detail") or body.get("error") or detail
    return detail


def _classify_stream_event(event: dict[str, Any]) -> tuple[str, Any]:
    """Map one decoded SSE event to ``(kind, value)`` for the stream loops."""
    if "delta" in event:
        return ("delta", event["delta"])
    if "usage" in event:
        return ("usage", event)
    if "error" in event:
        return ("error", event.get("detail") or event["error"])
    return ("skip", None)


def _decode_sse_line(line: str) -> dict[str, Any] | str | None:
    """Decode one SSE line into an event dict, the ``"[DONE]"`` sentinel, or None."""
    if not line or not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    if data == "[DONE]":
        return "[DONE]"
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


class RekAIClient:
    """Synchronous client.

    Example::

        from rekai_client import RekAIClient

        client = RekAIClient("http://localhost:8000")
        print(client.chat("echo", "hello").content)
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        *,
        provider_key: str | None = None,
        gateway_key: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._provider_key = provider_key
        self._gateway_key = gateway_key
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RekAIClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- helpers -----------------------------------------------------------
    def _headers(self, provider_key: str | None, gateway_key: str | None = None) -> dict[str, str]:
        return _build_headers(self._provider_key, self._gateway_key, provider_key, gateway_key)

    def _payload(
        self,
        model: str,
        messages: Messages,
        provider: str | None,
        temperature: float,
        max_tokens: int | None,
        cache: bool,
        fallbacks: list[dict[str, Any]] | None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _build_payload(
            model,
            messages,
            provider,
            temperature,
            max_tokens,
            cache,
            fallbacks,
            tools,
            tool_choice,
            response_format,
        )

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        try:
            body = resp.json()
        except Exception:
            body = None
        raise RekAIError(_detail_from_body(resp.status_code, body), status_code=resp.status_code)

    # -- API ---------------------------------------------------------------
    def chat(
        self,
        model: str,
        messages: Messages,
        *,
        provider: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        cache: bool = True,
        fallbacks: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        response_format: dict[str, Any] | None = None,
        provider_key: str | None = None,
        gateway_key: str | None = None,
    ) -> ChatResult:
        payload = self._payload(
            model,
            messages,
            provider,
            temperature,
            max_tokens,
            cache,
            fallbacks,
            tools,
            tool_choice,
            response_format,
        )
        resp = self._client.post(
            "/v1/chat", json=payload, headers=self._headers(provider_key, gateway_key)
        )
        self._raise_for_status(resp)
        return ChatResult.from_dict(resp.json())

    def stream(
        self,
        model: str,
        messages: Messages,
        *,
        provider: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        provider_key: str | None = None,
        gateway_key: str | None = None,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
    ) -> Iterator[str]:
        """Yield response text chunks from the streaming endpoint.

        If ``on_usage`` is given, it is called once with the final usage summary
        ``{"provider", "model", "usage", "cost_usd", "estimated"}`` when the
        server reports it (just before the stream ends).
        """
        payload = self._payload(
            model,
            messages,
            provider,
            temperature,
            max_tokens,
            True,
            None,
            response_format=response_format,
        )
        with self._client.stream(
            "POST",
            "/v1/chat/stream",
            json=payload,
            headers=self._headers(provider_key, gateway_key),
        ) as resp:
            self._raise_for_status(resp)
            for line in resp.iter_lines():
                decoded = _decode_sse_line(line)
                if decoded is None:
                    continue
                if decoded == "[DONE]":
                    return
                assert isinstance(decoded, dict)
                kind, value = _classify_stream_event(decoded)
                if kind == "delta":
                    yield value
                elif kind == "usage":
                    if on_usage is not None:
                        on_usage(value)
                elif kind == "error":
                    raise RekAIError(value)

    def embeddings(
        self,
        model: str,
        input: str | list[str],
        *,
        provider: str | None = None,
        cache: bool = True,
        provider_key: str | None = None,
        gateway_key: str | None = None,
    ) -> EmbeddingsResult:
        """Create embeddings for a string or list of strings."""
        payload: dict[str, Any] = {"model": model, "input": input, "cache": cache}
        if provider is not None:
            payload["provider"] = provider
        resp = self._client.post(
            "/v1/embeddings", json=payload, headers=self._headers(provider_key, gateway_key)
        )
        self._raise_for_status(resp)
        return EmbeddingsResult.from_dict(resp.json())

    def models(self, *, gateway_key: str | None = None) -> list[dict[str, str]]:
        resp = self._client.get("/v1/models", headers=self._headers(None, gateway_key))
        self._raise_for_status(resp)
        return resp.json().get("data", [])

    def usage(self, *, gateway_key: str | None = None) -> dict[str, Any]:
        resp = self._client.get("/v1/usage", headers=self._headers(None, gateway_key))
        self._raise_for_status(resp)
        return resp.json()

    def health(self) -> dict[str, Any]:
        resp = self._client.get("/health")
        self._raise_for_status(resp)
        return resp.json()


class AsyncRekAIClient:
    """Asynchronous client — an ``async``/``await`` mirror of :class:`RekAIClient`.

    Backed by ``httpx.AsyncClient`` so the same connection pool can be reused
    across awaits. Every method mirrors the synchronous surface; ``stream()`` is
    an async generator you drive with ``async for``.

    Example::

        from rekai_client import AsyncRekAIClient

        async with AsyncRekAIClient("http://localhost:8000") as client:
            result = await client.chat("echo", "hello")
            print(result.content)
            async for chunk in client.stream("echo", "hi"):
                print(chunk, end="")
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        *,
        provider_key: str | None = None,
        gateway_key: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._provider_key = provider_key
        self._gateway_key = gateway_key
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    # -- lifecycle ---------------------------------------------------------
    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncRekAIClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- helpers -----------------------------------------------------------
    def _headers(self, provider_key: str | None, gateway_key: str | None = None) -> dict[str, str]:
        return _build_headers(self._provider_key, self._gateway_key, provider_key, gateway_key)

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        try:
            body = resp.json()
        except Exception:
            body = None
        raise RekAIError(_detail_from_body(resp.status_code, body), status_code=resp.status_code)

    # -- API ---------------------------------------------------------------
    async def chat(
        self,
        model: str,
        messages: Messages,
        *,
        provider: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        cache: bool = True,
        fallbacks: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        response_format: dict[str, Any] | None = None,
        provider_key: str | None = None,
        gateway_key: str | None = None,
    ) -> ChatResult:
        payload = _build_payload(
            model,
            messages,
            provider,
            temperature,
            max_tokens,
            cache,
            fallbacks,
            tools,
            tool_choice,
            response_format,
        )
        resp = await self._client.post(
            "/v1/chat", json=payload, headers=self._headers(provider_key, gateway_key)
        )
        self._raise_for_status(resp)
        return ChatResult.from_dict(resp.json())

    async def stream(
        self,
        model: str,
        messages: Messages,
        *,
        provider: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        provider_key: str | None = None,
        gateway_key: str | None = None,
        on_usage: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> AsyncIterator[str]:
        """Yield response text chunks from the streaming endpoint.

        If ``on_usage`` is given, it is called once with the final usage summary
        when the server reports it. It may be a plain callable or a coroutine
        function (an awaitable return value is awaited).
        """
        payload = _build_payload(
            model,
            messages,
            provider,
            temperature,
            max_tokens,
            True,
            None,
            response_format=response_format,
        )
        async with self._client.stream(
            "POST",
            "/v1/chat/stream",
            json=payload,
            headers=self._headers(provider_key, gateway_key),
        ) as resp:
            if resp.status_code >= 400:
                # The error body isn't read yet on a streaming response; read it
                # so RekAIError carries the server's detail, not just the status.
                await resp.aread()
                self._raise_for_status(resp)
            async for line in resp.aiter_lines():
                decoded = _decode_sse_line(line)
                if decoded is None:
                    continue
                if decoded == "[DONE]":
                    return
                assert isinstance(decoded, dict)
                kind, value = _classify_stream_event(decoded)
                if kind == "delta":
                    yield value
                elif kind == "usage":
                    if on_usage is not None:
                        maybe = on_usage(value)
                        if maybe is not None:
                            await maybe
                elif kind == "error":
                    raise RekAIError(value)

    async def embeddings(
        self,
        model: str,
        input: str | list[str],
        *,
        provider: str | None = None,
        cache: bool = True,
        provider_key: str | None = None,
        gateway_key: str | None = None,
    ) -> EmbeddingsResult:
        """Create embeddings for a string or list of strings."""
        payload: dict[str, Any] = {"model": model, "input": input, "cache": cache}
        if provider is not None:
            payload["provider"] = provider
        resp = await self._client.post(
            "/v1/embeddings", json=payload, headers=self._headers(provider_key, gateway_key)
        )
        self._raise_for_status(resp)
        return EmbeddingsResult.from_dict(resp.json())

    async def models(self, *, gateway_key: str | None = None) -> list[dict[str, str]]:
        resp = await self._client.get("/v1/models", headers=self._headers(None, gateway_key))
        self._raise_for_status(resp)
        return resp.json().get("data", [])

    async def usage(self, *, gateway_key: str | None = None) -> dict[str, Any]:
        resp = await self._client.get("/v1/usage", headers=self._headers(None, gateway_key))
        self._raise_for_status(resp)
        return resp.json()

    async def health(self) -> dict[str, Any]:
        resp = await self._client.get("/health")
        self._raise_for_status(resp)
        return resp.json()
