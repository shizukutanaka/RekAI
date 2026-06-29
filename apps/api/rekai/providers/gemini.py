"""Google Gemini (Generative Language API) provider."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from rekai.config import get_settings
from rekai.providers.base import Provider, ProviderError, ProviderResult, StreamEvent
from rekai.schemas import ChatRequest, Usage


class GeminiProvider(Provider):
    name = "gemini"
    requires_key = True

    def server_key_configured(self) -> bool:
        return bool(get_settings().gemini_api_key)

    def _resolve_key(self, api_key: str | None) -> str:
        key = api_key or get_settings().gemini_api_key
        if not key:
            raise ProviderError(
                "No Gemini API key. Provide one with the 'X-Provider-Key' header (BYOK) "
                "or set REKAI_GEMINI_API_KEY.",
                status_code=401,
            )
        return key

    def _build_payload(self, request: ChatRequest) -> dict:
        # Gemini uses roles "user"/"model" and carries the system prompt separately.
        contents = []
        for m in request.messages:
            if m.role == "system":
                continue
            contents.append(
                {
                    "role": "model" if m.role == "assistant" else "user",
                    "parts": [{"text": m.content}],
                }
            )
        if not contents:
            raise ProviderError("Gemini requires at least one user/assistant message.", 400)

        payload: dict = {
            "contents": contents,
            "generationConfig": {"temperature": request.temperature},
        }
        if request.max_tokens is not None:
            payload["generationConfig"]["maxOutputTokens"] = request.max_tokens

        system_parts = [m.content for m in request.messages if m.role == "system"]
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        return payload

    async def chat(self, request: ChatRequest, api_key: str | None) -> ProviderResult:
        settings = get_settings()
        key = self._resolve_key(api_key)
        payload = self._build_payload(request)
        url = f"{settings.gemini_base_url.rstrip('/')}/models/{request.model}:generateContent"
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                resp = await client.post(url, json=payload, headers={"x-goog-api-key": key})
        except httpx.HTTPError as exc:
            raise ProviderError(f"Gemini request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise ProviderError(
                f"Gemini returned {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code if resp.status_code < 500 else 502,
            )

        data = resp.json()
        content = _extract_text(data)
        meta = data.get("usageMetadata", {})
        prompt_tokens = meta.get("promptTokenCount", 0)
        completion_tokens = meta.get("candidatesTokenCount", 0)
        return ProviderResult(
            content=content,
            model=request.model,
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=meta.get("totalTokenCount", prompt_tokens + completion_tokens),
            ),
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
        payload = self._build_payload(request)
        url = (
            f"{settings.gemini_base_url.rstrip('/')}/models/"
            f"{request.model}:streamGenerateContent?alt=sse"
        )
        last_usage: dict | None = None
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                async with client.stream(
                    "POST", url, json=payload, headers={"x-goog-api-key": key}
                ) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode()[:200]
                        raise ProviderError(
                            f"Gemini returned {resp.status_code}: {body}",
                            status_code=resp.status_code if resp.status_code < 500 else 502,
                        )
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:") :].strip()
                        if not data:
                            continue
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        text = _extract_text(chunk)
                        if text:
                            yield StreamEvent(delta=text)
                        if chunk.get("usageMetadata"):
                            last_usage = chunk["usageMetadata"]
        except httpx.HTTPError as exc:
            raise ProviderError(f"Gemini streaming request failed: {exc}") from exc
        if last_usage is not None:
            prompt_tokens = last_usage.get("promptTokenCount", 0)
            completion_tokens = last_usage.get("candidatesTokenCount", 0)
            yield StreamEvent(
                usage=Usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=last_usage.get(
                        "totalTokenCount", prompt_tokens + completion_tokens
                    ),
                )
            )

    async def list_models(self, api_key: str | None) -> list[str]:
        return [
            "gemini-2.0-flash",
            "gemini-1.5-pro",
            "gemini-1.5-flash",
        ]


def _extract_text(data: dict) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts)


def _parse_gemini_sse_line(line: str) -> str | None:
    """Extract the text delta from one Gemini SSE data line, if present."""
    if not line or not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    if not data:
        return None
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        return None
    return _extract_text(chunk) or None
