"""OpenAI (and OpenAI-compatible) chat completions provider."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from rekai.config import get_settings
from rekai.providers.base import Provider, ProviderError, ProviderResult
from rekai.schemas import ChatRequest, Usage


class OpenAIProvider(Provider):
    name = "openai"
    requires_key = True

    def server_key_configured(self) -> bool:
        return bool(get_settings().openai_api_key)

    async def chat(self, request: ChatRequest, api_key: str | None) -> ProviderResult:
        settings = get_settings()
        key = api_key or settings.openai_api_key
        if not key:
            raise ProviderError(
                "No OpenAI API key. Provide one with the 'X-Provider-Key' header (BYOK) "
                "or set REKAI_OPENAI_API_KEY.",
                status_code=401,
            )

        payload: dict = {
            "model": request.model,
            "messages": [m.model_dump() for m in request.messages],
            "temperature": request.temperature,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        url = f"{settings.openai_base_url.rstrip('/')}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                resp = await client.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {key}"},
                )
        except httpx.HTTPError as exc:  # network-level failure
            raise ProviderError(f"OpenAI request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise ProviderError(
                f"OpenAI returned {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code if resp.status_code < 500 else 502,
            )

        data = resp.json()
        choice = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return ProviderResult(
            content=choice,
            model=data.get("model", request.model),
            usage=Usage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            ),
        )

    async def stream(self, request: ChatRequest, api_key: str | None) -> AsyncIterator[str]:
        settings = get_settings()
        key = api_key or settings.openai_api_key
        if not key:
            raise ProviderError(
                "No OpenAI API key. Provide one with the 'X-Provider-Key' header (BYOK) "
                "or set REKAI_OPENAI_API_KEY.",
                status_code=401,
            )

        payload: dict = {
            "model": request.model,
            "messages": [m.model_dump() for m in request.messages],
            "temperature": request.temperature,
            "stream": True,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        url = f"{settings.openai_base_url.rstrip('/')}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                async with client.stream(
                    "POST", url, json=payload, headers={"Authorization": f"Bearer {key}"}
                ) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode()[:200]
                        raise ProviderError(
                            f"OpenAI returned {resp.status_code}: {body}",
                            status_code=resp.status_code if resp.status_code < 500 else 502,
                        )
                    async for line in resp.aiter_lines():
                        delta = _parse_openai_sse_line(line)
                        if delta:
                            yield delta
        except httpx.HTTPError as exc:
            raise ProviderError(f"OpenAI streaming request failed: {exc}") from exc

    async def list_models(self, api_key: str | None) -> list[str]:
        # Static, commonly-available models; avoids an extra network round-trip.
        return ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"]


def _parse_openai_sse_line(line: str) -> str | None:
    """Extract the text delta from one OpenAI SSE line, if present."""
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
    if not choices:
        return None
    return choices[0].get("delta", {}).get("content") or None
