"""A small synchronous client for the RekAI gateway."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
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
    def _headers(
        self, provider_key: str | None, gateway_key: str | None = None
    ) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = provider_key or self._provider_key
        if key:
            headers["X-Provider-Key"] = key
        # The gateway key authenticates this client to RekAI (REKAI_API_KEYS);
        # distinct from the provider key above, which is BYOK for the upstream
        # provider. Required on /v1/* whenever the deployment has keys configured.
        bearer = gateway_key or self._gateway_key
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return headers

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
        return payload

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        detail = f"RekAI returned {resp.status_code}"
        try:
            body = resp.json()
            detail = body.get("detail") or body.get("error") or detail
        except Exception:
            pass
        raise RekAIError(detail, status_code=resp.status_code)

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
        provider_key: str | None = None,
        gateway_key: str | None = None,
    ) -> ChatResult:
        payload = self._payload(
            model, messages, provider, temperature, max_tokens, cache, fallbacks, tools, tool_choice
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
        provider_key: str | None = None,
        gateway_key: str | None = None,
        on_usage: Callable[[dict[str, Any]], None] | None = None,
    ) -> Iterator[str]:
        """Yield response text chunks from the streaming endpoint.

        If ``on_usage`` is given, it is called once with the final usage summary
        ``{"provider", "model", "usage", "cost_usd", "estimated"}`` when the
        server reports it (just before the stream ends).
        """
        payload = self._payload(model, messages, provider, temperature, max_tokens, True, None)
        with self._client.stream(
            "POST",
            "/v1/chat/stream",
            json=payload,
            headers=self._headers(provider_key, gateway_key),
        ) as resp:
            self._raise_for_status(resp)
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    return
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if "delta" in event:
                    yield event["delta"]
                elif "usage" in event:
                    if on_usage is not None:
                        on_usage(event)
                elif "error" in event:
                    raise RekAIError(event.get("detail") or event["error"])

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
