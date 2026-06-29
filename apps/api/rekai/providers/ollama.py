"""Ollama provider — talks to a local Ollama server (no API key required)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from rekai.config import get_settings
from rekai.providers.base import Provider, ProviderError, ProviderResult
from rekai.schemas import ChatRequest, Usage


class OllamaProvider(Provider):
    name = "ollama"
    requires_key = False

    async def chat(self, request: ChatRequest, api_key: str | None) -> ProviderResult:
        settings = get_settings()
        payload = {
            "model": request.model,
            "messages": [m.model_dump() for m in request.messages],
            "stream": False,
            "options": {"temperature": request.temperature},
        }
        url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                resp = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Ollama request failed (is it running at {settings.ollama_base_url}?): {exc}"
            ) from exc

        if resp.status_code >= 400:
            raise ProviderError(
                f"Ollama returned {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code if resp.status_code < 500 else 502,
            )

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

    async def stream(self, request: ChatRequest, api_key: str | None) -> AsyncIterator[str]:
        settings = get_settings()
        payload = {
            "model": request.model,
            "messages": [m.model_dump() for m in request.messages],
            "stream": True,
            "options": {"temperature": request.temperature},
        }
        url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                async with client.stream("POST", url, json=payload) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode()[:200]
                        raise ProviderError(
                            f"Ollama returned {resp.status_code}: {body}",
                            status_code=resp.status_code if resp.status_code < 500 else 502,
                        )
                    async for line in resp.aiter_lines():
                        delta = _parse_ollama_ndjson_line(line)
                        if delta:
                            yield delta
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Ollama streaming request failed (is it running at "
                f"{settings.ollama_base_url}?): {exc}"
            ) from exc


def _parse_ollama_ndjson_line(line: str) -> str | None:
    """Extract the text delta from one Ollama NDJSON line, if present."""
    if not line.strip():
        return None
    try:
        chunk = json.loads(line)
    except json.JSONDecodeError:
        return None
    return chunk.get("message", {}).get("content") or None
