"""Ollama provider — talks to a local Ollama server (no API key required)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from rekai.config import get_settings
from rekai.logging_config import get_logger
from rekai.providers.base import (
    EmbeddingResult,
    FinishReason,
    Provider,
    ProviderError,
    ProviderResult,
    StreamEvent,
    provider_http_error,
    trace_headers,
)
from rekai.schemas import ChatRequest, Usage

logger = get_logger("rekai.providers.ollama")


def _warn_unsupported_fields(request: ChatRequest) -> None:
    """Log the request fields this provider can't honor, rather than dropping
    them without a trace."""
    if request.tools is not None:
        logger.debug("ignoring tools: not wired up for the ollama provider")


def _options(request: ChatRequest) -> dict:
    """Ollama's per-request generation options.

    ``num_predict`` is Ollama's spelling of ``max_tokens``. It was missing, so
    the cap was accepted by RekAI's schema, honored by the other three
    providers, and silently discarded here — the local model generated until it
    stopped on its own. Shared by the chat and streaming paths so the two cannot
    drift apart again.
    """
    options: dict = {"temperature": request.temperature}
    if request.max_tokens is not None:
        options["num_predict"] = request.max_tokens
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
        settings = get_settings()
        _warn_unsupported_fields(request)
        payload: dict = {
            "model": request.model,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
            "stream": False,
            "options": _options(request),
        }
        fmt = _response_format(request)
        if fmt is not None:
            payload["format"] = fmt
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
        content = data.get("message", {}).get("content", "")
        finish_reason = _finish_reason(data.get("done_reason"))
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
            finish_reason=finish_reason,
        )

    async def embed(self, inputs: list[str], model: str, api_key: str | None) -> EmbeddingResult:
        settings = get_settings()
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
        settings = get_settings()
        _warn_unsupported_fields(request)
        payload: dict = {
            "model": request.model,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
            "stream": True,
            "options": _options(request),
        }
        fmt = _response_format(request)
        if fmt is not None:
            payload["format"] = fmt
        url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
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
        finish_reason = _finish_reason(chunk.get("done_reason"))
        if prompt_tokens or completion_tokens or finish_reason:
            return StreamEvent(
                usage=(
                    Usage(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=prompt_tokens + completion_tokens,
                    )
                    if prompt_tokens or completion_tokens
                    else None
                ),
                finish_reason=finish_reason,
            )
    return None
