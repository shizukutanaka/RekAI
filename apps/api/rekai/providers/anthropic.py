"""Anthropic (Claude) Messages API provider."""

from __future__ import annotations

import httpx

from rekai.config import get_settings
from rekai.providers.base import Provider, ProviderError, ProviderResult
from rekai.schemas import ChatRequest, Usage


class AnthropicProvider(Provider):
    name = "anthropic"
    requires_key = True

    async def chat(self, request: ChatRequest, api_key: str | None) -> ProviderResult:
        settings = get_settings()
        key = api_key or settings.anthropic_api_key
        if not key:
            raise ProviderError(
                "No Anthropic API key. Provide one with the 'X-Provider-Key' header (BYOK) "
                "or set REKAI_ANTHROPIC_API_KEY.",
                status_code=401,
            )

        # Anthropic takes system prompts as a top-level field, not in `messages`.
        system_parts = [m.content for m in request.messages if m.role == "system"]
        chat_messages = [
            {"role": m.role, "content": m.content} for m in request.messages if m.role != "system"
        ]
        if not chat_messages:
            raise ProviderError("Anthropic requires at least one user/assistant message.", 400)

        payload: dict = {
            "model": request.model,
            "messages": chat_messages,
            "max_tokens": request.max_tokens or settings.anthropic_default_max_tokens,
            "temperature": request.temperature,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)

        url = f"{settings.anthropic_base_url.rstrip('/')}/messages"
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                resp = await client.post(
                    url,
                    json=payload,
                    headers={
                        "x-api-key": key,
                        "anthropic-version": settings.anthropic_version,
                        "content-type": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Anthropic request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise ProviderError(
                f"Anthropic returned {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code if resp.status_code < 500 else 502,
            )

        data = resp.json()
        # content is a list of blocks; concatenate the text blocks.
        content = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
        usage = data.get("usage", {})
        prompt_tokens = usage.get("input_tokens", 0)
        completion_tokens = usage.get("output_tokens", 0)
        return ProviderResult(
            content=content,
            model=data.get("model", request.model),
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )

    async def list_models(self, api_key: str | None) -> list[str]:
        return [
            "claude-opus-4-8",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
        ]
