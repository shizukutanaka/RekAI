"""Anthropic (Claude) Messages API provider."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from rekai.config import get_settings
from rekai.providers.base import Provider, ProviderError, ProviderResult, StreamEvent
from rekai.schemas import ChatRequest, Usage


class AnthropicProvider(Provider):
    name = "anthropic"
    requires_key = True

    def server_key_configured(self) -> bool:
        return bool(get_settings().anthropic_api_key)

    def _resolve_key(self, api_key: str | None) -> str:
        key = api_key or get_settings().anthropic_api_key
        if not key:
            raise ProviderError(
                "No Anthropic API key. Provide one with the 'X-Provider-Key' header (BYOK) "
                "or set REKAI_ANTHROPIC_API_KEY.",
                status_code=401,
            )
        return key

    def _build_payload(self, request: ChatRequest, *, stream: bool) -> dict:
        settings = get_settings()
        # Anthropic takes system prompts as a top-level field, not in `messages`.
        system_parts = [m.content or "" for m in request.messages if m.role == "system"]
        chat_messages = _translate_messages(request.messages)
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
        # Translate OpenAI-style tools / tool_choice into Anthropic's format.
        if request.tools:
            payload["tools"] = _translate_tools(request.tools)
            choice = _translate_tool_choice(request.tool_choice)
            if choice is not None:
                payload["tool_choice"] = choice
        if stream:
            payload["stream"] = True
        return payload

    def _headers(self, key: str) -> dict[str, str]:
        return {
            "x-api-key": key,
            "anthropic-version": get_settings().anthropic_version,
            "content-type": "application/json",
        }

    async def chat(self, request: ChatRequest, api_key: str | None) -> ProviderResult:
        settings = get_settings()
        key = self._resolve_key(api_key)
        payload = self._build_payload(request, stream=False)

        url = f"{settings.anthropic_base_url.rstrip('/')}/messages"
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                resp = await client.post(url, json=payload, headers=self._headers(key))
        except httpx.HTTPError as exc:
            raise ProviderError(f"Anthropic request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise ProviderError(
                f"Anthropic returned {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code if resp.status_code < 500 else 502,
            )

        data = resp.json()
        blocks = data.get("content", [])
        # content is a list of blocks; concatenate the text blocks.
        content = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        tool_calls = _extract_tool_calls(blocks)
        usage = data.get("usage", {})
        prompt_tokens = usage.get("input_tokens", 0)
        completion_tokens = usage.get("output_tokens", 0)
        return ProviderResult(
            content=content,
            model=data.get("model", request.model),
            tool_calls=tool_calls,
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
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
        payload = self._build_payload(request, stream=True)
        url = f"{settings.anthropic_base_url.rstrip('/')}/messages"
        # input tokens arrive in message_start; output tokens in message_delta.
        input_tokens = 0
        output_tokens = 0
        saw_usage = False
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
                async with client.stream(
                    "POST", url, json=payload, headers=self._headers(key)
                ) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode()[:200]
                        raise ProviderError(
                            f"Anthropic returned {resp.status_code}: {body}",
                            status_code=resp.status_code if resp.status_code < 500 else 502,
                        )
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:") :].strip()
                        if not data:
                            continue
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        etype = event.get("type")
                        if etype == "content_block_delta":
                            text = event.get("delta", {}).get("text")
                            if text:
                                yield StreamEvent(delta=text)
                        elif etype == "message_start":
                            usage = event.get("message", {}).get("usage", {})
                            input_tokens = usage.get("input_tokens", input_tokens)
                            output_tokens = usage.get("output_tokens", output_tokens)
                            saw_usage = True
                        elif etype == "message_delta":
                            usage = event.get("usage", {})
                            if "output_tokens" in usage:
                                output_tokens = usage["output_tokens"]
                                saw_usage = True
        except httpx.HTTPError as exc:
            raise ProviderError(f"Anthropic streaming request failed: {exc}") from exc
        if saw_usage:
            yield StreamEvent(
                usage=Usage(
                    prompt_tokens=input_tokens,
                    completion_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                )
            )

    async def list_models(self, api_key: str | None) -> list[str]:
        return [
            "claude-opus-4-8",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
        ]


def _parse_anthropic_sse_line(line: str) -> str | None:
    """Extract the text delta from one Anthropic SSE data line, if present."""
    if not line or not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    if not data:
        return None
    try:
        event = json.loads(data)
    except json.JSONDecodeError:
        return None
    if event.get("type") == "content_block_delta":
        return event.get("delta", {}).get("text") or None
    return None


# --- OpenAI <-> Anthropic tool translation ----------------------------------


def _translate_tools(openai_tools: list[dict]) -> list[dict]:
    """OpenAI ``{"type":"function","function":{name,description,parameters}}``
    -> Anthropic ``{name, description, input_schema}``."""
    out = []
    for tool in openai_tools:
        fn = tool.get("function", tool)
        out.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out


def _translate_tool_choice(choice: object) -> dict | None:
    """OpenAI tool_choice -> Anthropic tool_choice (None means leave default)."""
    if choice is None or choice == "none":
        return None
    if choice == "auto":
        return {"type": "auto"}
    if choice == "required":
        return {"type": "any"}
    if isinstance(choice, dict):
        name = choice.get("function", {}).get("name") or choice.get("name")
        if name:
            return {"type": "tool", "name": name}
    return None


def _translate_messages(messages: list) -> list[dict]:
    """Map OpenAI-style messages (incl. tool calls/results) to Anthropic blocks."""
    out: list[dict] = []
    for m in messages:
        if m.role == "system":
            continue
        if m.role == "tool":
            # A tool result becomes a user message with a tool_result block.
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.tool_call_id or "",
                            "content": m.content or "",
                        }
                    ],
                }
            )
        elif m.role == "assistant" and m.tool_calls:
            blocks: list[dict] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args,
                    }
                )
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": m.role, "content": m.content or ""})
    return out


def _extract_tool_calls(blocks: list[dict]) -> list[dict] | None:
    """Anthropic ``tool_use`` content blocks -> OpenAI-style ``tool_calls``."""
    calls = []
    for b in blocks:
        if b.get("type") == "tool_use":
            calls.append(
                {
                    "id": b.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": b.get("name", ""),
                        "arguments": json.dumps(b.get("input", {})),
                    },
                }
            )
    return calls or None
