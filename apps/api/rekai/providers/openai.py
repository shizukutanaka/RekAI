"""OpenAI (and OpenAI-compatible) chat completions provider."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from rekai import models
from rekai.config import get_settings
from rekai.providers.base import (
    EmbeddingResult,
    Provider,
    ProviderError,
    ProviderResult,
    StreamEvent,
    provider_http_error,
    trace_headers,
)
from rekai.schemas import ChatRequest, Usage


class OpenAIProvider(Provider):
    name = "openai"
    requires_key = True

    # --- overridable hooks (subclassed for OpenAI-compatible backends) -----
    def _base_url(self) -> str:
        return get_settings().openai_base_url

    def _server_key(self) -> str | None:
        return get_settings().openai_api_key

    def _key_env_hint(self) -> str:
        return "REKAI_OPENAI_API_KEY"

    def _resolve_key(self, api_key: str | None) -> str:
        key = api_key or self._server_key()
        if not key:
            raise ProviderError(
                f"No {self.name} API key. Provide one with the 'X-Provider-Key' header "
                f"(BYOK) or set {self._key_env_hint()}.",
                status_code=401,
            )
        return key

    def server_key_configured(self) -> bool:
        return bool(self._server_key())

    async def chat(self, request: ChatRequest, api_key: str | None) -> ProviderResult:
        settings = get_settings()
        key = self._resolve_key(api_key)

        payload: dict = {
            "model": request.model,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
            "temperature": request.temperature,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.tools is not None:
            payload["tools"] = request.tools
        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice
        if request.response_format is not None:
            payload["response_format"] = request.response_format

        url = f"{self._base_url().rstrip('/')}/chat/completions"
        try:
            client = self._client(settings.request_timeout_seconds)
            resp = await client.post(
                url,
                json=payload,
                headers={**trace_headers(), "Authorization": f"Bearer {key}"},
            )
        except httpx.HTTPError as exc:  # network-level failure
            raise ProviderError(f"{self.name} request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise provider_http_error(self.name, resp.status_code, resp.text, resp.headers)

        data = resp.json()
        message = data["choices"][0]["message"]
        usage = data.get("usage", {})
        return ProviderResult(
            content=message.get("content") or "",
            model=data.get("model", request.model),
            usage=Usage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                # OpenAI caches prompts automatically and reports how much of
                # prompt_tokens was served from cache (already included in it,
                # unlike Anthropic). There is no separate write count.
                cache_read_tokens=_cached_prompt_tokens(usage),
            ),
            tool_calls=message.get("tool_calls"),
        )

    async def stream(self, request: ChatRequest, api_key: str | None) -> AsyncIterator[str]:
        async for ev in self.stream_events(request, api_key):
            if ev.delta:
                yield ev.delta

    async def stream_events(
        self, request: ChatRequest, api_key: str | None
    ) -> AsyncIterator[StreamEvent]:
        settings = get_settings()
        key = self._resolve_key(api_key)

        payload: dict = {
            "model": request.model,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
            "temperature": request.temperature,
            "stream": True,
            # Ask for a final usage chunk for accurate accounting.
            "stream_options": {"include_usage": True},
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.tools is not None:
            payload["tools"] = request.tools
        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice
        if request.response_format is not None:
            payload["response_format"] = request.response_format

        url = f"{self._base_url().rstrip('/')}/chat/completions"
        tool_calls_acc: dict[int, dict] = {}
        try:
            client = self._client(settings.request_timeout_seconds)
            async with client.stream(
                "POST",
                url,
                json=payload,
                headers={**trace_headers(), "Authorization": f"Bearer {key}"},
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode()[:200]
                    raise ProviderError(
                        f"{self.name} returned {resp.status_code}: {body}",
                        status_code=resp.status_code if resp.status_code < 500 else 502,
                    )
                async for line in resp.aiter_lines():
                    event = _parse_openai_sse_event(line)
                    if event is not None:
                        yield event
                    _accumulate_tool_call_deltas(line, tool_calls_acc)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name} streaming request failed: {exc}") from exc
        if tool_calls_acc:
            assembled = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
            yield StreamEvent(tool_calls=assembled)

    async def embed(self, inputs: list[str], model: str, api_key: str | None) -> EmbeddingResult:
        settings = get_settings()
        key = self._resolve_key(api_key)
        url = f"{self._base_url().rstrip('/')}/embeddings"
        try:
            client = self._client(settings.request_timeout_seconds)
            resp = await client.post(
                url,
                json={"model": model, "input": inputs},
                headers={**trace_headers(), "Authorization": f"Bearer {key}"},
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name} embeddings request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise provider_http_error(self.name, resp.status_code, resp.text, resp.headers)
        data = resp.json()
        rows = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
        usage = data.get("usage", {})
        return EmbeddingResult(
            embeddings=[r["embedding"] for r in rows],
            model=data.get("model", model),
            usage=Usage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            ),
        )

    async def list_models(self, api_key: str | None) -> list[str]:
        # Sourced from the single model registry (rekai/models.py), which also
        # feeds the router and the price table so the three can't drift apart.
        return models.advertised_models("openai", "chat")

    async def list_embedding_models(self, api_key: str | None) -> list[str]:
        return models.advertised_models("openai", "embedding")


def _cached_prompt_tokens(usage: dict) -> int:
    """Cached prompt tokens from an OpenAI usage object (0 when absent)."""
    details = usage.get("prompt_tokens_details") or {}
    return details.get("cached_tokens", 0) or 0


def _parse_openai_sse_line(line: str) -> str | None:
    """Extract the text delta from one OpenAI SSE line, if present."""
    event = _parse_openai_sse_event(line)
    return event.delta if event else None


def _parse_openai_sse_event(line: str) -> StreamEvent | None:
    """Parse one OpenAI SSE line into a text delta or a final usage event."""
    if not line or not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    if not data or data == "[DONE]":
        return None
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        return None
    choices = chunk.get("choices") or []
    if choices:
        delta = choices[0].get("delta", {}).get("content")
        if delta:
            return StreamEvent(delta=delta)
    usage = chunk.get("usage")
    if usage:
        return StreamEvent(
            usage=Usage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                cache_read_tokens=_cached_prompt_tokens(usage),
            )
        )
    return None


def _accumulate_tool_call_deltas(line: str, acc: dict[int, dict]) -> None:
    """Merge one SSE line's streamed tool-call fragments into ``acc`` by index.

    OpenAI streams tool calls as deltas: the id/name arrive once, and the
    function ``arguments`` string is split across chunks.
    """
    if not line or not line.startswith("data:"):
        return
    data = line[len("data:") :].strip()
    if not data or data == "[DONE]":
        return
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        return
    choices = chunk.get("choices") or []
    if not choices:
        return
    for tc in choices[0].get("delta", {}).get("tool_calls") or []:
        idx = tc.get("index", 0)
        slot = acc.setdefault(
            idx, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}}
        )
        if tc.get("id"):
            slot["id"] = tc["id"]
        if tc.get("type"):
            slot["type"] = tc["type"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]
