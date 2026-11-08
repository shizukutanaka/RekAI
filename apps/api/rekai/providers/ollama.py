"""Ollama provider — talks to a local Ollama server (no API key required)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from rekai.config import current_settings
from rekai.logging_config import get_logger
from rekai.providers.base import (
    EmbeddingResult,
    FinishReason,
    Provider,
    ProviderError,
    ProviderResult,
    StreamEvent,
    openai_chat_message,
    provider_http_error,
    trace_headers,
)
from rekai.schemas import ChatMessage, ChatRequest, Usage

logger = get_logger("rekai.providers.ollama")


def _warn_unsupported_fields(request: ChatRequest) -> None:
    """Log the request fields this provider can't honor, rather than dropping
    them without a trace."""
    if isinstance(request.tool_choice, dict) or request.tool_choice == "required":
        logger.debug("ignoring tool_choice: not supported by the ollama provider")
    if request.parallel_tool_calls is False:
        logger.debug("ignoring parallel_tool_calls: not supported by the ollama provider")


def _options(request: ChatRequest) -> dict:
    """Ollama's per-request generation options.

    ``num_predict`` is Ollama's spelling of ``max_tokens``. It was missing, so
    the cap was accepted by RekAI's schema, honored by the other three
    providers, and silently discarded here — the local model generated until it
    stopped on its own. Shared by the chat and streaming paths so the two cannot
    drift apart again.
    """
    options: dict = {"temperature": request.temperature}
    max_tokens = request.max_tokens or request.max_completion_tokens
    if max_tokens is not None:
        options["num_predict"] = max_tokens
    if request.stop:
        options["stop"] = request.stop
    if request.top_p is not None:
        options["top_p"] = request.top_p
    if request.seed is not None:
        options["seed"] = request.seed
    if request.ollama_options:
        # Provider-native escape hatch for Ollama's many runner options. It is
        # merged last so it can intentionally override shared-field mappings
        # (e.g. a caller can set num_ctx without inventing another RekAI alias).
        options.update(request.ollama_options)
    return options


def _response_format(request: ChatRequest) -> str | dict | None:
    """Map OpenAI's ``response_format`` onto Ollama's top-level ``format``.

    ``json_object`` → ``"json"`` (free-form JSON); ``json_schema`` → the schema
    itself, which Ollama uses for constrained decoding so the output conforms by
    construction rather than by instruction. Anything else (``{"type": "text"}``
    or a shape RekAI doesn't recognise) returns None and is left off the payload.
    """
    rf = request.response_format
    if not isinstance(rf, dict):
        return None
    kind = rf.get("type")
    if kind == "json_schema":
        schema = (rf.get("json_schema") or {}).get("schema")
        if schema is not None:
            return schema
        return "json"  # json_schema with no schema still means "JSON, please"
    if kind == "json_object":
        return "json"
    return None


def _arguments_object(arguments: object) -> dict:
    """Normalize OpenAI's string arguments back to Ollama's object form."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _arguments_json(arguments: object) -> str:
    """Normalize Ollama's object arguments to OpenAI's string form."""
    if isinstance(arguments, dict):
        return json.dumps(arguments)
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
        if isinstance(parsed, dict):
            return json.dumps(parsed)
        return arguments
    return "{}"


def _ollama_history_tool_call(tool_call: dict, index: int) -> dict:
    function = tool_call.get("function")
    fn = function if isinstance(function, dict) else {}
    name = fn.get("name") or tool_call.get("name") or ""
    return {
        "type": "function",
        "function": {
            "index": index,
            "name": name,
            "arguments": _arguments_object(fn.get("arguments", tool_call.get("arguments", {}))),
        },
    }


def _ollama_messages(messages: list[ChatMessage]) -> list[dict]:
    """Serialize RekAI messages to Ollama's native tool-calling shape.

    Ollama matches tool results by ``tool_name`` rather than OpenAI's
    ``tool_call_id``, and its assistant ``tool_calls`` carry object arguments.
    Resolve call ids to names from the preceding assistant turn so a normal
    OpenAI-shaped history still round-trips.
    """
    tool_names: dict[str, str] = {}
    for message in messages:
        if message.role != "assistant" or not message.tool_calls:
            continue
        for tool_call in message.tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            fn = function if isinstance(function, dict) else {}
            call_id = tool_call.get("id")
            name = fn.get("name") or tool_call.get("name")
            if call_id and name:
                tool_names[str(call_id)] = str(name)

    out: list[dict] = []
    for message in messages:
        if message.role == "tool":
            name = (
                message.name
                or tool_names.get(message.tool_call_id or "")
                or message.tool_call_id
                or "tool"
            )
            out.append({"role": "tool", "tool_name": name, "content": message.content or ""})
            continue
        if message.role == "assistant" and message.tool_calls:
            payload: dict = {"role": "assistant"}
            if message.content is not None:
                payload["content"] = message.content
            payload["tool_calls"] = [
                _ollama_history_tool_call(tool_call, index)
                for index, tool_call in enumerate(message.tool_calls)
                if isinstance(tool_call, dict)
            ]
            _set_message_thinking(payload, message)
            out.append(payload)
            continue
        if message.role == "assistant":
            payload = openai_chat_message(message)
            _set_message_thinking(payload, message)
            out.append(payload)
            continue
        out.append(openai_chat_message(message))
    return out


def _request_tools(request: ChatRequest) -> list[dict] | None:
    if request.tool_choice == "none":
        return None
    return request.tools


def _request_think(request: ChatRequest) -> bool | str | None:
    """Ollama's top-level ``think`` value, or None to leave the model default."""
    think = request.think
    if think is None or (isinstance(think, str) and not think):
        return None
    return think


def _apply_request_think(payload: dict, request: ChatRequest) -> None:
    think = _request_think(request)
    if think is not None:
        payload["think"] = think


def _message_thinking(message: ChatMessage) -> str | None:
    """Ollama's assistant ``thinking`` string from normalized thinking blocks."""
    if not message.thinking_blocks:
        return None
    parts: list[str] = []
    for block in message.thinking_blocks:
        if not isinstance(block, dict) or block.get("type") != "thinking":
            continue
        text = block.get("thinking")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts) or None


def _set_message_thinking(payload: dict, message: ChatMessage) -> None:
    thinking = _message_thinking(message)
    if thinking is not None:
        payload["thinking"] = thinking


def _extract_ollama_tool_calls(raw: object) -> list[dict] | None:
    """Ollama ``message.tool_calls`` -> OpenAI-style ``tool_calls``."""
    if not isinstance(raw, list):
        return None
    calls: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        fn = function if isinstance(function, dict) else item
        name = fn.get("name") or item.get("name") or ""
        calls.append(
            {
                "id": f"call_{name or 'fn'}_{i}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": _arguments_json(fn.get("arguments", item.get("arguments", {}))),
                },
            }
        )
    return calls or None


def _finish_reason(raw: object) -> FinishReason | None:
    """Map Ollama's ``done_reason`` onto the normalized vocabulary.

    Ollama reports ``"stop"`` when the model finished and ``"length"`` when it
    hit the context/prediction limit — already the right words, so this only
    validates and drops anything unrecognised.
    """
    if raw in ("stop", "length"):
        return raw  # type: ignore[return-value]
    return None


class OllamaProvider(Provider):
    name = "ollama"
    requires_key = False

    async def chat(self, request: ChatRequest, api_key: str | None) -> ProviderResult:
        settings = current_settings()
        _warn_unsupported_fields(request)
        payload: dict = {
            "model": request.model,
            "messages": _ollama_messages(request.messages),
            "stream": False,
            "options": _options(request),
        }
        tools = _request_tools(request)
        if tools:
            payload["tools"] = tools
        fmt = _response_format(request)
        if fmt is not None:
            payload["format"] = fmt
        _apply_request_think(payload, request)
        url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
        try:
            client = self._client(settings.request_timeout_seconds)
            resp = await client.post(url, json=payload, headers=trace_headers())
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Ollama request failed (is it running at {settings.ollama_base_url}?): {exc}"
            ) from exc

        if resp.status_code >= 400:
            raise provider_http_error("Ollama", resp.status_code, resp.text, resp.headers)

        data = resp.json()
        message = data.get("message") or {}
        content = message.get("content") or ""
        tool_calls = _extract_ollama_tool_calls(message.get("tool_calls"))
        thinking = message.get("thinking")
        thinking_blocks = (
            [{"type": "thinking", "thinking": thinking}]
            if isinstance(thinking, str) and thinking
            else None
        )
        finish_reason = _finish_reason(data.get("done_reason"))
        if tool_calls and finish_reason == "stop":
            finish_reason = "tool_calls"
        prompt_tokens = data.get("prompt_eval_count", 0)
        completion_tokens = data.get("eval_count", 0)
        return ProviderResult(
            content=content,
            model=data.get("model", request.model),
            tool_calls=tool_calls,
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            finish_reason=finish_reason,
            thinking_blocks=thinking_blocks,
        )

    async def embed(
        self,
        inputs: list[str],
        model: str,
        api_key: str | None,
        *,
        dimensions: int | None = None,
        encoding_format: str | None = None,
        user: str | None = None,
    ) -> EmbeddingResult:
        settings = current_settings()
        url = f"{settings.ollama_base_url.rstrip('/')}/api/embed"
        try:
            client = self._client(settings.request_timeout_seconds)
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
        settings = current_settings()
        _warn_unsupported_fields(request)
        payload: dict = {
            "model": request.model,
            "messages": _ollama_messages(request.messages),
            "stream": True,
            "options": _options(request),
        }
        tools = _request_tools(request)
        if tools:
            payload["tools"] = tools
        fmt = _response_format(request)
        if fmt is not None:
            payload["format"] = fmt
        _apply_request_think(payload, request)
        url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
        tool_calls_acc: list[dict] = []
        try:
            client = self._client(settings.request_timeout_seconds)
            async with client.stream("POST", url, json=payload, headers=trace_headers()) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode()[:200]
                    raise ProviderError(
                        f"Ollama returned {resp.status_code}: {body}",
                        status_code=resp.status_code if resp.status_code < 500 else 502,
                    )
                async for line in resp.aiter_lines():
                    event = _parse_ollama_ndjson_event(line)
                    if event is None:
                        continue
                    if event.tool_calls:
                        tool_calls_acc.extend(event.tool_calls)
                        event.tool_calls = None
                    if tool_calls_acc and event.finish_reason == "stop":
                        event.finish_reason = "tool_calls"
                    if (
                        event.delta is None
                        and event.thinking_delta is None
                        and event.usage is None
                        and event.finish_reason is None
                        and event.model is None
                    ):
                        continue
                    yield event
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Ollama streaming request failed (is it running at "
                f"{settings.ollama_base_url}?): {exc}"
            ) from exc
        if tool_calls_acc:
            # Ollama has no call ids; synthesize them across the whole stream —
            # per-chunk indices would collide when the same function is called
            # from more than one NDJSON event.
            for index, tool_call in enumerate(tool_calls_acc):
                function = tool_call.get("function")
                fn = function if isinstance(function, dict) else {}
                name = fn.get("name") or tool_call.get("name") or "fn"
                tool_call["id"] = f"call_{name}_{index}"
            yield StreamEvent(tool_calls=tool_calls_acc)


def _parse_ollama_ndjson_event(line: str) -> StreamEvent | None:
    """Parse one Ollama NDJSON line into deltas, thinking, tool calls, usage."""
    if not line.strip():
        return None
    try:
        chunk = json.loads(line)
    except json.JSONDecodeError:
        return None
    message = chunk.get("message") or {}
    content = message.get("content")
    thinking = message.get("thinking")
    thinking_delta = thinking if isinstance(thinking, str) and thinking else None
    tool_calls = _extract_ollama_tool_calls(message.get("tool_calls"))
    done = bool(chunk.get("done"))
    usage: Usage | None = None
    finish_reason: FinishReason | None = None
    if done:
        prompt_tokens = chunk.get("prompt_eval_count", 0)
        completion_tokens = chunk.get("eval_count", 0)
        finish_reason = _finish_reason(chunk.get("done_reason"))
        if prompt_tokens or completion_tokens:
            usage = Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            )
    delta = content if content else None
    model = chunk.get("model")
    if (
        delta is None
        and thinking_delta is None
        and tool_calls is None
        and usage is None
        and finish_reason is None
        and model is None
    ):
        return None
    return StreamEvent(
        model=model if isinstance(model, str) else None,
        delta=delta,
        thinking_delta=thinking_delta,
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=finish_reason,
    )
