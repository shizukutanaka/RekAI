"""Ollama provider — talks to a local Ollama server (no API key required)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

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


class OllamaProvider(Provider):
    name = "ollama"
    requires_key = False

    async def chat(self, request: ChatRequest, api_key: str | None) -> ProviderResult:
        settings = get_settings()
        payload = {
            "model": request.model,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
            "stream": False,
            "options": {"temperature": request.temperature},
        }
        url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                resp = await client.post(url, json=payload, headers=trace_headers())
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Ollama request failed (is it running at {settings.ollama_base_url}?): {exc}"
            ) from exc

        if resp.status_code >= 400:
            raise provider_http_error("Ollama", resp.status_code, resp.text, resp.headers)

        data = resp.json()
        content = data.get("message", {}).get("content", "")
        prompt_tokens = data.get("prompt_eval_count", 0)
        completion_tokens = data.get("eval_count", 0)
        return ProviderResult(
            content=content,
            model=data.get("model", request.model),
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )

    async def embed(self, inputs: list[str], model: str, api_key: str | None) -> EmbeddingResult:
        settings = get_settings()
        url = f"{settings.ollama_base_url.rstrip('/')}/api/embed"
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                resp = await client.post(
                    url, json={"model": model, "input": inputs}, headers=trace_headers()
                )
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Ollama embeddings request failed "
                f"(is it running at {settings.ollama_base_url}?): {exc}"
            ) from exc
        if resp.status_code >= 400:
            raise provider_http_error("Ollama", resp.status_code, resp.text, resp.headers)
        data = resp.json()
        prompt_tokens = data.get("prompt_eval_count", 0)
        return EmbeddingResult(
            embeddings=data.get("embeddings", []),
            model=data.get("model", model),
            usage=Usage(prompt_tokens=prompt_tokens, total_tokens=prompt_tokens),
        )

    async def stream(self, request: ChatRequest, api_key: str | None) -> AsyncIterator[str]:
        async for ev in self.stream_events(request, api_key):
            if ev.delta:
                yield ev.delta

    async def stream_events(
        self, request: ChatRequest, api_key: str | None
    ) -> AsyncIterator[StreamEvent]:
        settings = get_settings()
        payload = {
            "model": request.model,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
            "stream": True,
            "options": {"temperature": request.temperature},
        }
        url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                async with client.stream(
                    "POST", url, json=payload, headers=trace_headers()
                ) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode()[:200]
                        raise ProviderError(
                            f"Ollama returned {resp.status_code}: {body}",
                            status_code=resp.status_code if resp.status_code < 500 else 502,
                        )
                    async for line in resp.aiter_lines():
                        event = _parse_ollama_ndjson_event(line)
                        if event is not None:
                            yield event
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Ollama streaming request failed (is it running at "
                f"{settings.ollama_base_url}?): {exc}"
            ) from exc


def _parse_ollama_ndjson_line(line: str) -> str | None:
    """Extract the text delta from one Ollama NDJSON line, if present."""
    event = _parse_ollama_ndjson_event(line)
    return event.delta if event else None


def _parse_ollama_ndjson_event(line: str) -> StreamEvent | None:
    """Parse one Ollama NDJSON line into a text delta or a final usage event."""
    if not line.strip():
        return None
    try:
        chunk = json.loads(line)
    except json.JSONDecodeError:
        return None
    content = chunk.get("message", {}).get("content")
    if content:
        return StreamEvent(delta=content)
    if chunk.get("done"):
        prompt_tokens = chunk.get("prompt_eval_count", 0)
        completion_tokens = chunk.get("eval_count", 0)
        if prompt_tokens or completion_tokens:
            return StreamEvent(
                usage=Usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                )
            )
    return None
