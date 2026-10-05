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
    CompletionUsage,
    ContentPart,
    OpenAIChatMessage,
    PromptTokensDetails,
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
    # OpenAI's pre-tools function-calling fields (deprecated but still sent by
    # older SDKs) are normalized onto tools/tool_choice — providers only speak
    # the modern names. Modern fields win when a caller sends both.
    tools = req.tools
    if tools is None and req.functions:
        tools = [{"type": "function", "function": f} for f in req.functions]
    tool_choice = req.tool_choice
    if tool_choice is None and req.function_call is not None:
        fc = req.function_call
        if isinstance(fc, str):
            tool_choice = fc  # "auto" / "none" pass through unchanged
        elif isinstance(fc, dict) and fc.get("name"):
            tool_choice = {"type": "function", "function": {"name": fc["name"]}}
    # OpenAI accepts `stop` as a bare string; ChatRequest.stop is declared
    # list[str] | None, so mypy needs this widened before construction even
    # though ChatRequest's own before-validator would normalize it at runtime
    # regardless of the caller.
    stop = [req.stop] if isinstance(req.stop, str) else req.stop
    return ChatRequest(
        model=model,
        messages=[_to_chat_message(m) for m in req.messages],
        provider=provider,
        temperature=temperature,
        # OpenAI renamed max_tokens -> max_completion_tokens; accept either.
        max_tokens=req.max_tokens or req.max_completion_tokens,
        stop=stop,
        top_p=req.top_p,
        seed=req.seed,
        frequency_penalty=req.frequency_penalty,
        presence_penalty=req.presence_penalty,
        logit_bias=req.logit_bias,
        service_tier=req.service_tier,
        web_search_options=req.web_search_options,
        include_obfuscation=(
            req.stream_options.include_obfuscation if req.stream_options else None
        ),
        cache=req.cache,
        fallbacks=req.fallbacks,
        tools=tools,
        tool_choice=tool_choice,
        parallel_tool_calls=req.parallel_tool_calls,
        response_format=req.response_format,
        user=req.user,
        safety_identifier=req.safety_identifier,
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
                    refusal=resp.refusal,
                    annotations=resp.annotations,
                ),
                # The provider's own reason when it gave one. The fallback
                # is the old behavior, kept only for responses that predate
                # finish_reason (cached entries, a provider that reports
                # nothing) — synthesising it for everything is what made a
                # truncated answer indistinguishable from a complete one.
                finish_reason=resp.finish_reason or ("tool_calls" if resp.tool_calls else "stop"),
            )
        ],
        # OpenAI nests both breakdowns — the cached-token count under
        # prompt_tokens_details, reasoning under completion_tokens_details;
        # emit each only when the provider reported one, like OpenAI does.
        usage=CompletionUsage(
            **resp.usage.model_dump(),
            prompt_tokens_details=(
                PromptTokensDetails(cached_tokens=resp.usage.cache_read_tokens)
                if resp.usage.cache_read_tokens
                else None
            ),
            completion_tokens_details=(
                {"reasoning_tokens": resp.usage.reasoning_tokens}
                if resp.usage.reasoning_tokens
                else None
            ),
        ),
        provider=resp.provider,
        system_fingerprint=resp.system_fingerprint,
        service_tier=resp.service_tier,
        cost_usd=resp.cost_usd,
        cached=resp.cached,
        fallback_used=resp.fallback_used,
    )


# --- streaming chunk builders (chat.completion.chunk) ----------------------


def _chunk_base(
    chunk_id: str,
    created: int,
    model: str,
    system_fingerprint: str | None = None,
    service_tier: str | None = None,
) -> dict:
    chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
    }
    # OpenAI stamps both on every chunk once known (the role announcement
    # carries them already); ours arrive with the first provider event, so
    # they appear on chunks from that point on.
    if system_fingerprint is not None:
        chunk["system_fingerprint"] = system_fingerprint
    if service_tier is not None:
        chunk["service_tier"] = service_tier
    return chunk


def chunk_first(chunk_id: str, created: int, model: str) -> dict:
    chunk = _chunk_base(chunk_id, created, model)
    chunk["choices"] = [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
    return chunk


def chunk_delta(
    chunk_id: str,
    created: int,
    model: str,
    text: str,
    system_fingerprint: str | None = None,
    service_tier: str | None = None,
) -> dict:
    chunk = _chunk_base(chunk_id, created, model, system_fingerprint, service_tier)
    chunk["choices"] = [{"index": 0, "delta": {"content": text}, "finish_reason": None}]
    return chunk


def chunk_refusal(chunk_id: str, created: int, model: str, text: str) -> dict:
    # OpenAI streams refusal text in `delta.refusal`, parallel to content deltas.
    chunk = _chunk_base(chunk_id, created, model)
    chunk["choices"] = [{"index": 0, "delta": {"refusal": text}, "finish_reason": None}]
    return chunk


def chunk_annotations(chunk_id: str, created: int, model: str, annotations: list[dict]) -> dict:
    # OpenAI streams annotations (e.g. web-search url_citations) complete inside
    # one delta chunk — SDKs append them onto the assembled message.
    chunk = _chunk_base(chunk_id, created, model)
    chunk["choices"] = [{"index": 0, "delta": {"annotations": annotations}, "finish_reason": None}]
    return chunk


def chunk_tool_calls(
    chunk_id: str,
    created: int,
    model: str,
    tool_calls: list[dict],
    system_fingerprint: str | None = None,
    service_tier: str | None = None,
) -> dict:
    # The internal pipeline yields fully-assembled tool calls in one shot; OpenAI
    # streaming requires an index per call, so attach one. A single chunk with
    # complete arguments is valid — SDKs reassemble by index either way.
    indexed = [{**tc, "index": i} for i, tc in enumerate(tool_calls)]
    chunk = _chunk_base(chunk_id, created, model, system_fingerprint, service_tier)
    chunk["choices"] = [{"index": 0, "delta": {"tool_calls": indexed}, "finish_reason": None}]
    return chunk


def chunk_finish(
    chunk_id: str,
    created: int,
    model: str,
    reason: str,
    system_fingerprint: str | None = None,
    service_tier: str | None = None,
) -> dict:
    chunk = _chunk_base(chunk_id, created, model, system_fingerprint, service_tier)
    chunk["choices"] = [{"index": 0, "delta": {}, "finish_reason": reason}]
    return chunk


def chunk_usage(
    chunk_id: str,
    created: int,
    model: str,
    usage: Usage,
    system_fingerprint: str | None = None,
    service_tier: str | None = None,
) -> dict:
    # Per OpenAI's stream_options.include_usage: a final chunk with an empty
    # choices array and the usage totals.
    chunk = _chunk_base(chunk_id, created, model, system_fingerprint, service_tier)
    chunk["choices"] = []
    body = usage.model_dump()
    if usage.cache_read_tokens:
        body["prompt_tokens_details"] = {"cached_tokens": usage.cache_read_tokens}
    if usage.reasoning_tokens:
        body["completion_tokens_details"] = {"reasoning_tokens": usage.reasoning_tokens}
    chunk["usage"] = body
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


def openai_error(
    status_code: int,
    message: str,
    param: str | None = None,
    code: str | None = None,
    error_type: str | None = None,
) -> dict:
    """The OpenAI error envelope, so SDK error handling parses RekAI's errors.

    ``param`` names the offending request field when exactly one is at fault —
    OpenAI populates it for a bad or missing parameter, and the SDK exposes it
    as ``exc.param``. It stays None otherwise, as OpenAI leaves it.

    ``code`` carries OpenAI's machine-readable codes (``model_not_found``,
    ``context_length_exceeded``…) when the caller knows the specific one, and
    ``error_type`` overrides the status-derived type when OpenAI's real
    response disagrees with it (e.g. an unknown model is a 404 whose type is
    ``invalid_request_error``, not ``api_error``).
    """
    return {
        "error": {
            "message": message,
            "type": error_type or _error_type_for_status(status_code),
            "param": param,
            "code": code,
        }
    }
