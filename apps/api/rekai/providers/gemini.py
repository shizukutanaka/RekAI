"""Google Gemini (Generative Language API) provider."""

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
        contents = _translate_contents(request.messages)
        if not contents:
            raise ProviderError("Gemini requires at least one user/assistant message.", 400)

        payload: dict = {
            "contents": contents,
            "generationConfig": {"temperature": request.temperature},
        }
        if request.max_tokens is not None:
            payload["generationConfig"]["maxOutputTokens"] = request.max_tokens

        # Best-effort structured output: OpenAI's response_format maps onto
        # Gemini's generationConfig. json_object -> JSON mime type; json_schema
        # additionally constrains the shape via responseSchema.
        rf = request.response_format
        if isinstance(rf, dict) and rf.get("type") in ("json_object", "json_schema"):
            payload["generationConfig"]["responseMimeType"] = "application/json"
            schema = (rf.get("json_schema") or {}).get("schema")
            if schema is not None:
                payload["generationConfig"]["responseSchema"] = schema

        system_parts = [m.content or "" for m in request.messages if m.role == "system"]
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}

        # Translate OpenAI-style tools / tool_choice into Gemini's format.
        if request.tools:
            payload["tools"] = _translate_tools(request.tools)
            config = _translate_tool_config(request.tool_choice)
            if config is not None:
                payload["toolConfig"] = config
        return payload

    async def chat(self, request: ChatRequest, api_key: str | None) -> ProviderResult:
        settings = get_settings()
        key = self._resolve_key(api_key)
        payload = self._build_payload(request)
        url = f"{settings.gemini_base_url.rstrip('/')}/models/{request.model}:generateContent"
        try:
            client = self._client(settings.request_timeout_seconds)
            resp = await client.post(
                url, json=payload, headers={**trace_headers(), "x-goog-api-key": key}
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Gemini request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise provider_http_error("Gemini", resp.status_code, resp.text, resp.headers)

        data = resp.json()
        content = _extract_text(data)
        tool_calls = _extract_tool_calls(data)
        meta = data.get("usageMetadata", {})
        prompt_tokens = meta.get("promptTokenCount", 0)
        completion_tokens = meta.get("candidatesTokenCount", 0)
        return ProviderResult(
            content=content,
            model=request.model,
            tool_calls=tool_calls,
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=meta.get("totalTokenCount", prompt_tokens + completion_tokens),
            ),
        )

    async def embed(self, inputs: list[str], model: str, api_key: str | None) -> EmbeddingResult:
        settings = get_settings()
        key = self._resolve_key(api_key)
        # Gemini wants the fully-qualified model name in each request.
        qualified = model if model.startswith("models/") else f"models/{model}"
        payload = {
            "requests": [
                {"model": qualified, "content": {"parts": [{"text": text}]}} for text in inputs
            ]
        }
        url = f"{settings.gemini_base_url.rstrip('/')}/{qualified}:batchEmbedContents"
        try:
            client = self._client(settings.request_timeout_seconds)
            resp = await client.post(
                url, json=payload, headers={**trace_headers(), "x-goog-api-key": key}
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Gemini embeddings request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise provider_http_error("Gemini", resp.status_code, resp.text, resp.headers)
        data = resp.json()
        embeddings = [row.get("values", []) for row in data.get("embeddings", [])]
        return EmbeddingResult(embeddings=embeddings, model=model)

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
        # Gemini streams a functionCall complete within a chunk; collect across chunks.
        tool_calls: list[dict] = []
        try:
            client = self._client(settings.request_timeout_seconds)
            async with client.stream(
                "POST", url, json=payload, headers={**trace_headers(), "x-goog-api-key": key}
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
                    calls = _extract_tool_calls(chunk)
                    if calls:
                        tool_calls.extend(calls)
                    if chunk.get("usageMetadata"):
                        last_usage = chunk["usageMetadata"]
        except httpx.HTTPError as exc:
            raise ProviderError(f"Gemini streaming request failed: {exc}") from exc
        if tool_calls:
            # Re-id sequentially since per-chunk indices may collide.
            for i, call in enumerate(tool_calls):
                call["id"] = f"call_{call['function']['name'] or 'fn'}_{i}"
            yield StreamEvent(tool_calls=tool_calls)
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

    async def list_embedding_models(self, api_key: str | None) -> list[str]:
        return ["text-embedding-004"]


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


# --- OpenAI <-> Gemini tool translation -------------------------------------


def _translate_tools(openai_tools: list[dict]) -> list[dict]:
    """OpenAI tools -> Gemini ``[{"functionDeclarations": [...]}]``."""
    declarations = []
    for tool in openai_tools:
        fn = tool.get("function", tool)
        decl = {"name": fn.get("name", ""), "description": fn.get("description", "")}
        params = fn.get("parameters")
        if params:
            decl["parameters"] = params
        declarations.append(decl)
    return [{"functionDeclarations": declarations}]


def _translate_tool_config(choice: object) -> dict | None:
    """OpenAI tool_choice -> Gemini ``toolConfig.functionCallingConfig``."""
    if choice is None:
        return None
    mode = "AUTO"
    allowed: list[str] | None = None
    if choice == "auto":
        mode = "AUTO"
    elif choice == "required":
        mode = "ANY"
    elif choice == "none":
        mode = "NONE"
    elif isinstance(choice, dict):
        name = choice.get("function", {}).get("name") or choice.get("name")
        mode = "ANY"
        allowed = [name] if name else None
    config: dict = {"mode": mode}
    if allowed:
        config["allowedFunctionNames"] = allowed
    return {"functionCallingConfig": config}


def _translate_contents(messages: list) -> list[dict]:
    """Map OpenAI-style messages (incl. tool calls/results) to Gemini contents."""
    out: list[dict] = []
    for m in messages:
        if m.role == "system":
            continue
        if m.role == "tool":
            out.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": m.name or "",
                                "response": {"result": m.content or ""},
                            }
                        }
                    ],
                }
            )
        elif m.role == "assistant" and m.tool_calls:
            parts: list[dict] = []
            if m.content:
                parts.append({"text": m.content})
            for tc in m.tool_calls:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                parts.append({"functionCall": {"name": fn.get("name", ""), "args": args}})
            out.append({"role": "model", "parts": parts})
        else:
            out.append(
                {
                    "role": "model" if m.role == "assistant" else "user",
                    "parts": [{"text": m.content or ""}],
                }
            )
    return out


def _extract_tool_calls(data: dict) -> list[dict] | None:
    """Gemini ``functionCall`` parts -> OpenAI-style ``tool_calls`` (ids synthesized)."""
    candidates = data.get("candidates") or []
    if not candidates:
        return None
    parts = candidates[0].get("content", {}).get("parts", [])
    calls = []
    for i, p in enumerate(parts):
        fc = p.get("functionCall")
        if fc:
            calls.append(
                {
                    "id": f"call_{fc.get('name', 'fn')}_{i}",
                    "type": "function",
                    "function": {
                        "name": fc.get("name", ""),
                        "arguments": json.dumps(fc.get("args", {})),
                    },
                }
            )
    return calls or None
