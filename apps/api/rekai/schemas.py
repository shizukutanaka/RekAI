"""Pydantic request/response models — these define the public OpenAPI schema."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Role = Literal["system", "user", "assistant", "tool"]


class ChatMessage(BaseModel):
    role: Role
    # Optional because assistant tool-call messages carry tool_calls instead.
    content: str | None = None
    name: str | None = None
    # Pass-through OpenAI-style tool fields (round-tripping tool calls).
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    # Provider-native prompt-cache breakpoint, passed through verbatim (e.g.
    # Anthropic's {"type": "ephemeral"}). Providers that cache automatically
    # (OpenAI) ignore it.
    cache_control: dict[str, Any] | None = None
    # Anthropic thinking/redacted_thinking blocks echoed back in assistant
    # history (verbatim dicts). Providers without the concept drop them.
    thinking_blocks: list[dict[str, Any]] | None = None


class FallbackTarget(BaseModel):
    provider: str = Field(..., description="Provider to fall back to.")
    model: str | None = Field(
        default=None, description="Model for the fallback; defaults to the request model."
    )


class ChatRequest(BaseModel):
    model: str = Field(..., description="Model name, e.g. 'gpt-4o-mini' or 'echo'.")
    messages: list[ChatMessage] = Field(..., min_length=1)
    provider: str | None = Field(
        default=None,
        description="Force a provider. If omitted, RekAI routes by model name / default.",
    )
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    stop: list[str] | None = Field(
        default=None,
        description="Sequences that stop generation, as OpenAI's `stop`. A bare "
        "string is accepted and normalized to a one-element list. Forwarded to "
        "every provider under its own name; a provider's own limit (OpenAI "
        "allows 4) surfaces as that provider's error.",
    )
    service_tier: str | None = Field(
        default=None,
        description="OpenAI's processing tier ('auto' | 'default' | 'flex' | "
        "'priority' | 'scale'): flex trades latency for a large discount, "
        "priority pays for lower latency. Not enum-validated so newer tiers "
        "stay forward-compatible; an unsupported tier surfaces as the "
        "provider's own error. Forwarded to OpenAI-compatible providers only.",
    )
    web_search_options: dict | None = Field(
        default=None,
        description="OpenAI's `web_search_options` — search context size, "
        "user location, etc. for models with hosted web search. Forwarded "
        "verbatim to OpenAI-compatible providers only.",
    )
    anthropic_beta: str | None = Field(
        default=None,
        description="Anthropic `anthropic-beta` header value (comma-joined "
        "beta flags). Set by the Anthropic-compat route from the incoming "
        "header; forwarded upstream so beta-gated features work. In the "
        "cache key — beta flags can change the response.",
    )
    thinking: dict[str, Any] | None = Field(
        default=None,
        description="Anthropic's `thinking` config — e.g. {'type': 'enabled', "
        "'budget_tokens': 4096} enables extended thinking. Forwarded verbatim "
        "to Anthropic only; other providers ignore it. Anthropic requires "
        "temperature=1 under thinking, so the compat layer defaults to that "
        "when the caller left temperature unset.",
    )
    include_obfuscation: bool | None = Field(
        default=None,
        description="OpenAI's `stream_options.include_obfuscation`: asks the "
        "provider to obfuscate streamed tokens. Forwarded into the upstream "
        "`stream_options` on OpenAI-compatible providers only.",
    )
    cache: bool = Field(default=True, description="Whether this request may be served from cache.")
    fallbacks: list[FallbackTarget] | None = Field(
        default=None,
        description="Ordered fallbacks tried on upstream (5xx) errors. "
        "Overrides the server default chain.",
    )
    tools: list[dict[str, Any]] | None = Field(
        default=None,
        description="OpenAI-style tool/function definitions, passed through to the provider.",
    )
    tool_choice: Any | None = Field(
        default=None,
        description="Tool choice ('auto' | 'none' | 'required' | {...}), passed through.",
    )
    response_format: dict[str, Any] | None = Field(
        default=None,
        description="OpenAI-style response_format, e.g. {'type': 'json_object'} or "
        "{'type': 'json_schema', 'json_schema': {...}}. Passed through to providers "
        "that support it (OpenAI/OpenAI-compatible natively, Gemini best-effort); "
        "ignored by others.",
    )
    cache_control: dict[str, Any] | None = Field(
        default=None,
        description="Provider-native prompt-cache breakpoint applied to the last "
        "prompt block, e.g. {'type': 'ephemeral'}. Anthropic honors it (cached "
        "prompt prefixes are billed at a large discount); OpenAI caches "
        "automatically and ignores it. Per-message placement is also supported "
        "via a message's own cache_control.",
    )

    @field_validator("stop", mode="before")
    @classmethod
    def _normalize_stop(cls, v: object) -> object:
        """Accept OpenAI's `str | list[str]` and hand every provider a list.

        Normalizing here rather than in each provider is deliberate: four
        payload builders each remembering to widen a string is exactly the
        duplication that let Ollama miss `max_tokens`. Empty strings are dropped
        — they would stop generation immediately — and an empty result becomes
        None so it is omitted from the payload rather than sent as `[]`.
        """
        if v is None:
            return None
        items = [v] if isinstance(v, str) else v
        if not isinstance(items, list):
            return v  # let pydantic report the type error
        kept = [s for s in items if isinstance(s, str) and s]
        return kept or None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # Provider-side prompt-cache accounting. Cached prompt tokens are billed at a
    # steep discount (Anthropic ~0.1x to read, ~1.25x to write); these are a
    # *breakdown* of prompt_tokens, not additional tokens. Default 0 so existing
    # responses and stored snapshots are unchanged.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # Reasoning/thinking tokens (OpenAI o- and gpt-5-series, Gemini thinking
    # models): a *breakdown* of completion_tokens, not additional tokens. 0 for
    # providers that don't report one (Anthropic folds thinking into
    # output_tokens without a separate count).
    reasoning_tokens: int = 0


# --- OpenAI-compatible /v1/chat/completions -------------------------------
# These mirror OpenAI's ChatCompletions API so RekAI is a drop-in base_url for
# the OpenAI SDKs, LangChain, etc. They are translated to/from the internal
# ChatRequest/ChatResponse in rekai/openai_compat.py.


class ContentPart(BaseModel):
    """One element of OpenAI's content-parts array form of a message."""

    type: str
    text: str | None = None


class OpenAIChatMessage(BaseModel):
    role: Role
    # OpenAI allows either a plain string or an array of typed content parts.
    content: str | list[ContentPart] | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class StreamOptions(BaseModel):
    include_usage: bool = False
    include_obfuscation: bool | None = None


class ChatCompletionsRequest(BaseModel):
    # Tolerate unknown OpenAI tuning params (frequency_penalty, seed, logit_bias,
    # ...) rather than 422-ing — matches vLLM/LiteLLM leniency for drop-in use.
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[OpenAIChatMessage] = Field(..., min_length=1)
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    stop: str | list[str] | None = None
    service_tier: str | None = None
    web_search_options: dict[str, Any] | None = None
    stream: bool = False
    stream_options: StreamOptions | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    response_format: dict[str, Any] | None = None
    user: str | None = None  # accepted, ignored
    n: int | None = None  # 400 if n > 1 (RekAI returns a single choice)
    provider: str | None = None  # RekAI extension: explicit provider override
    # OpenAI's pre-tools function-calling API (deprecated since 0613 but still
    # emitted by older SDKs and codebases). Normalized to tools/tool_choice in
    # the compat layer; modern `tools` wins when both are sent.
    functions: list[dict[str, Any]] | None = None
    function_call: Any | None = None


# --- Anthropic Messages API (`POST /v1/messages`) --------------------------
# These mirror Anthropic's Messages API so RekAI is a drop-in `base_url` for
# the Anthropic SDKs too. They are translated to/from the internal
# ChatRequest/ChatResponse in rekai/anthropic_compat.py.


class AnthropicContentBlock(BaseModel):
    """One element of a Messages-API content array (text, tool_use, tool_result,
    ...). Fields beyond `type` stay loose — the compat layer validates the
    ones it maps and rejects the rest with a readable 400."""

    model_config = ConfigDict(extra="allow")

    type: str
    text: str | None = None
    id: str | None = None  # tool_use
    name: str | None = None  # tool_use
    input: dict[str, Any] | None = None  # tool_use
    tool_use_id: str | None = None  # tool_result
    content: str | list[dict[str, Any]] | None = None  # tool_result body


class AnthropicMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str | list[AnthropicContentBlock]


class AnthropicTool(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)


class AnthropicToolChoice(BaseModel):
    type: Literal["auto", "none", "any", "tool"]
    name: str | None = None
    # disable_parallel_tool_use is Anthropic-specific; the OpenAI-equivalent
    # flag (parallel_tool_calls) lives on the request, not on tool_choice.


class _AnthropicMessagesBase(BaseModel):
    """Fields shared by `/v1/messages` and `/v1/messages/count_tokens` —
    everything except `max_tokens`, which the counter doesn't require."""

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[AnthropicMessage] = Field(..., min_length=1)
    system: str | list[dict[str, Any]] | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=1.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    stop_sequences: list[str] | None = None
    stream: bool = False
    tools: list[AnthropicTool] | None = None
    tool_choice: AnthropicToolChoice | None = None
    # Anthropic's extended-thinking config, verbatim ({'type': 'enabled',
    # 'budget_tokens': N}). Declared so it isn't swallowed by extra=allow.
    thinking: dict[str, Any] | None = None
    provider: str | None = None  # RekAI extension: explicit provider override


class AnthropicMessagesRequest(_AnthropicMessagesBase):
    """`POST /v1/messages` body. max_tokens is required by Anthropic (unlike
    OpenAI) and stays required here — an SDK caller always sends it."""

    max_tokens: int = Field(..., ge=1)


class AnthropicCountTokensRequest(_AnthropicMessagesBase):
    """`POST /v1/messages/count_tokens` body — the Messages shape except
    ``max_tokens`` is optional (Anthropic's counter doesn't need it)."""

    max_tokens: int | None = Field(default=None, ge=1)


class AnthropicTokenCount(BaseModel):
    """`POST /v1/messages/count_tokens` response — Anthropic's shape."""

    input_tokens: int


class ChatCompletionMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatCompletionMessage
    # "length" matters most: it is how an OpenAI client learns the answer was
    # truncated by max_tokens and should be retried with a larger budget.
    finish_reason: Literal["stop", "length", "tool_calls", "content_filter"] = "stop"


class CompletionUsage(Usage):
    # OpenAI reports the reasoning-token breakdown nested under
    # `completion_tokens_details` — internal Usage keeps it flat, the compat
    # surface re-nests it for SDK parity.
    completion_tokens_details: dict | None = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: CompletionUsage  # field names already match OpenAI's
    system_fingerprint: str | None = None
    # RekAI extensions — OpenAI SDKs ignore unknown response fields.
    provider: str | None = None
    cost_usd: float | None = None
    cached: bool = False
    fallback_used: bool = False


class ChatResponse(BaseModel):
    id: str
    provider: str
    model: str
    content: str
    tool_calls: list[dict[str, Any]] | None = Field(
        default=None, description="Tool calls returned by the model, if any."
    )
    usage: Usage
    cost_usd: float | None = Field(
        default=None,
        description="Approximate USD cost. 0.0 for free/local providers, null if unknown.",
    )
    cached: bool = False
    finish_reason: Literal["stop", "length", "tool_calls", "content_filter"] | None = Field(
        default=None,
        description="Why the model stopped, normalized across providers: 'stop' "
        "(finished), 'length' (cut off by max_tokens — the answer is INCOMPLETE), "
        "'tool_calls', or 'content_filter'. Null when the provider didn't report "
        "one, which is also how responses cached before this field existed read.",
    )
    cache_similarity: float | None = Field(
        default=None,
        description="Cosine similarity to the stored prompt when the semantic cache "
        "served this response — meaning the answer is to a *similar* prompt, not this "
        "one. Null on a miss and on an exact cache hit (where the prompt matched "
        "byte-for-byte), so a non-null value is exactly the signal that an "
        "approximate match was used.",
    )
    thinking_blocks: list[dict[str, Any]] | None = Field(
        default=None,
        description="Anthropic thinking/redacted_thinking blocks the model "
        "produced before its answer, verbatim (text + signature). Present only "
        "when thinking was enabled; the /v1/messages surface re-emits them as "
        "content blocks so the caller can echo them back verbatim.",
    )
    fallback_used: bool = Field(
        default=False, description="True if a fallback served this response, not the primary."
    )
    redacted: list[str] | None = Field(
        default=None,
        description="Names of the secret patterns scrubbed from 'content' by the output "
        "redaction guardrail, or null if nothing was redacted. Mirrors the X-Redacted "
        "header, and survives a cache hit or Idempotency-Key replay because redaction "
        "runs before the response is stored.",
    )
    created: int


class EmbeddingsRequest(BaseModel):
    model: str = Field(..., description="Embedding model, e.g. 'text-embedding-3-small' or 'echo'.")
    input: str | list[str] = Field(..., description="A string or list of strings to embed.")
    provider: str | None = Field(default=None, description="Force a provider (else routed).")
    cache: bool = Field(default=True)
    dimensions: int | None = Field(
        default=None,
        ge=1,
        description="Output dimensionality for models that support it "
        "(OpenAI text-embedding-3+, Gemini embedding models via "
        "`outputDimensionality`). Forwarded verbatim; an unsupported value "
        "surfaces as the provider's own error.",
    )
    encoding_format: str | None = Field(
        default=None,
        description="OpenAI's `encoding_format` ('float' | 'base64'). "
        "Forwarded to OpenAI-compatible providers only; note the API's own "
        "response stays JSON floats either way.",
    )


class EmbeddingsResponse(BaseModel):
    provider: str
    model: str
    embeddings: list[list[float]]
    usage: Usage
    cost_usd: float | None = None
    cached: bool = False


class ModelPricing(BaseModel):
    input_per_1m: float
    output_per_1m: float


class ModelInfo(BaseModel):
    id: str
    provider: str
    type: Literal["chat", "embedding"] = "chat"
    pricing: ModelPricing | None = None


class ModelsResponse(BaseModel):
    data: list[ModelInfo]


class ServiceInfo(BaseModel):
    name: str
    version: str
    description: str
    docs: str
    health: str


class ClientUsage(BaseModel):
    requests: int
    tokens: int
    cost_usd: float


class UsageSummary(BaseModel):
    requests_total: int
    cache_hits_total: int
    cache_misses_total: int
    semantic_cache_hits_total: int = Field(
        default=0,
        description="Subset of cache_hits_total served by approximate (embedding) "
        "match rather than an exact prompt match.",
    )
    errors_total: int
    fallbacks_total: int
    retries_total: int = 0
    cooldowns_total: int = 0
    tokens_total: int
    cost_usd_total: float
    requests_by_provider: dict[str, int]
    tokens_by_provider: dict[str, int] = Field(
        default_factory=dict,
        description="Tokens accounted per provider — same bounded key set as requests_by_provider.",
    )
    usage_by_client: dict[str, ClientUsage] = Field(
        default_factory=dict,
        description="Per-tenant usage keyed by a masked client id ('key:<hash>' "
        "when gateway auth is on, else the client IP).",
    )


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"] = Field(
        description="'degraded' when at least one provider is parked in cooldown "
        "after a 429 or repeated 5xx — the gateway is still serving, but not from "
        "every backend. Never a failure state: /health answering at all is the "
        "liveness signal, so it stays 200 either way."
    )
    version: str
    providers: list[str]
    provider_status: dict[str, Literal["ready", "byok_only"]] = Field(
        default_factory=dict,
        description="Per-provider readiness: 'ready' (usable now) or "
        "'byok_only' (needs an X-Provider-Key).",
    )
    parked_providers: dict[str, float] = Field(
        default_factory=dict,
        description="Providers currently in cooldown → seconds remaining. This "
        "worker's local view; a cooldown recorded by another replica in Redis "
        "isn't reflected (reading it would mean I/O, which /health avoids).",
    )
    cache: str


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None


class AdminKeyRequest(BaseModel):
    key: str = Field(..., min_length=1, description="The raw API key to add.")


class AdminKeyList(BaseModel):
    static: list[str] = Field(description="Masked REKAI_API_KEYS entries.")
    dynamic: list[str] = Field(description="Masked runtime-added keys.")


class AdminKeyResponse(BaseModel):
    status: Literal["added", "revoked"]
    key: str
