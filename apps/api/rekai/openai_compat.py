"""Translation between the OpenAI ChatCompletions shape and RekAI's internal
chat schema.

Pure functions only — no I/O. The route in ``main.py`` handles the OpenAI-
compatible ``POST /v1/chat/completions`` endpoint by translating the request
here, running it through the same internal pipeline as ``/v1/chat``, and
translating the result back. This keeps RekAI a drop-in ``base_url`` for the
OpenAI SDKs without duplicating any routing/cache/retry/fallback logic.
"""

from __future__ import annotations

from rekai.providers import get_provider
from rekai.providers.base import ProviderError
from rekai.schemas import (
    ChatCompletionChoice,
    ChatCompletionMessage,
    ChatCompletionResponse,
    ChatCompletionsRequest,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ContentPart,
    OpenAIChatMessage,
    Usage,
)

# Default temperature when the OpenAI request omits it (ChatRequest's default).
_DEFAULT_TEMPERATURE = 0.7


def _flatten_content(content: str | list[ContentPart] | None) -> str | None:
    """Reduce OpenAI's string-or-content-parts message body to plain text.

    RekAI's providers are text-only, so a non-text part (``image_url``, etc.)
    is a 400 rather than being silently dropped."""
    if content is None or isinstance(content, str):
        return content
    texts: list[str] = []
    for part in content:
        if part.type == "text" and part.text is not None:
            texts.append(part.text)
        else:
            raise ProviderError(
                f"Unsupported content part type '{part.type}'; RekAI providers accept text only.",
                status_code=400,
            )
    return "\n".join(texts)


def _to_chat_message(m: OpenAIChatMessage) -> ChatMessage:
    return ChatMessage(
        role=m.role,
        content=_flatten_content(m.content),
        name=m.name,
        tool_calls=m.tool_calls,
        tool_call_id=m.tool_call_id,
    )


def _resolve_provider_and_model(req: ChatCompletionsRequest) -> tuple[str | None, str]:
    """Decide the (provider, model) pair from an OpenAI request.

    Precedence: an explicit ``provider`` extension field wins; otherwise, if the
    model is ``"<provider>/<model>"`` and ``<provider>`` is a registered
    provider (OpenRouter-style), split it; otherwise pass the model through
    untouched so RekAI's prefix rules / default provider apply. The registered-
    prefix check is deliberate — a custom backend's own model ids can contain
    slashes (e.g. ``meta-llama/Llama-3-70b``) and must not be split."""
    if req.provider:
        return req.provider, req.model
    if "/" in req.model:
        prefix, rest = req.model.split("/", 1)
        if rest and get_provider(prefix) is not None:
            return prefix, rest
    return None, req.model


def to_chat_request(req: ChatCompletionsRequest) -> ChatRequest:
    """Translate an OpenAI ChatCompletions request into RekAI's ChatRequest."""
    if req.n is not None and req.n > 1:
        raise ProviderError(
            "RekAI returns a single choice; 'n' > 1 is not supported.",
            status_code=400,
        )
    provider, model = _resolve_provider_and_model(req)
    temperature = req.temperature if req.temperature is not None else _DEFAULT_TEMPERATURE
    return ChatRequest(
        model=model,
        messages=[_to_chat_message(m) for m in req.messages],
        provider=provider,
        temperature=temperature,
        # OpenAI renamed max_tokens -> max_completion_tokens; accept either.
        max_tokens=req.max_tokens or req.max_completion_tokens,
        cache=True,
        tools=req.tools,
        tool_choice=req.tool_choice,
        response_format=req.response_format,
    )


def to_chat_completion(resp: ChatResponse) -> ChatCompletionResponse:
    """Translate RekAI's ChatResponse into an OpenAI ChatCompletion object."""
    return ChatCompletionResponse(
        id=resp.id,
        created=resp.created,
        model=resp.model,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatCompletionMessage(
                    role="assistant",
                    content=resp.content or None,
                    tool_calls=resp.tool_calls,
                ),
                # The provider's own reason when it gave one. The fallback
                # is the old behavior, kept only for responses that predate
                # finish_reason (cached entries, a provider that reports
                # nothing) — synthesising it for everything is what made a
                # truncated answer indistinguishable from a complete one.
                finish_reason=resp.finish_reason or ("tool_calls" if resp.tool_calls else "stop"),
            )
        ],
        usage=resp.usage,
        provider=resp.provider,
        cost_usd=resp.cost_usd,
        cached=resp.cached,
        fallback_used=resp.fallback_used,
    )


# --- streaming chunk builders (chat.completion.chunk) ----------------------


def _chunk_base(chunk_id: str, created: int, model: str) -> dict:
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
    }


def chunk_first(chunk_id: str, created: int, model: str) -> dict:
    chunk = _chunk_base(chunk_id, created, model)
    chunk["choices"] = [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
    return chunk


def chunk_delta(chunk_id: str, created: int, model: str, text: str) -> dict:
    chunk = _chunk_base(chunk_id, created, model)
    chunk["choices"] = [{"index": 0, "delta": {"content": text}, "finish_reason": None}]
    return chunk


def chunk_tool_calls(chunk_id: str, created: int, model: str, tool_calls: list[dict]) -> dict:
    # The internal pipeline yields fully-assembled tool calls in one shot; OpenAI
    # streaming requires an index per call, so attach one. A single chunk with
    # complete arguments is valid — SDKs reassemble by index either way.
    indexed = [{**tc, "index": i} for i, tc in enumerate(tool_calls)]
    chunk = _chunk_base(chunk_id, created, model)
    chunk["choices"] = [{"index": 0, "delta": {"tool_calls": indexed}, "finish_reason": None}]
    return chunk


def chunk_finish(chunk_id: str, created: int, model: str, reason: str) -> dict:
    chunk = _chunk_base(chunk_id, created, model)
    chunk["choices"] = [{"index": 0, "delta": {}, "finish_reason": reason}]
    return chunk


def chunk_usage(chunk_id: str, created: int, model: str, usage: Usage) -> dict:
    # Per OpenAI's stream_options.include_usage: a final chunk with an empty
    # choices array and the usage totals.
    chunk = _chunk_base(chunk_id, created, model)
    chunk["choices"] = []
    chunk["usage"] = usage.model_dump()
    return chunk


# --- error envelope --------------------------------------------------------


def _error_type_for_status(status_code: int) -> str:
    if status_code == 401:
        return "authentication_error"
    if status_code in (400, 422):
        return "invalid_request_error"
    if status_code == 429:
        return "rate_limit_error"
    return "api_error"


def openai_error(status_code: int, message: str, param: str | None = None) -> dict:
    """The OpenAI error envelope, so SDK error handling parses RekAI's errors.

    ``param`` names the offending request field when exactly one is at fault —
    OpenAI populates it for a bad or missing parameter, and the SDK exposes it
    as ``exc.param``. It stays None otherwise, as OpenAI leaves it.
    """
    return {
        "error": {
            "message": message,
            "type": _error_type_for_status(status_code),
            "param": param,
            "code": None,
        }
    }
