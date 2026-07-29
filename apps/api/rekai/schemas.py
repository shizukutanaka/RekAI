"""Pydantic request/response models — these define the public OpenAPI schema."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class ChatCompletionsRequest(BaseModel):
    # Tolerate unknown OpenAI tuning params (frequency_penalty, seed, logit_bias,
    # ...) rather than 422-ing — matches vLLM/LiteLLM leniency for drop-in use.
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[OpenAIChatMessage] = Field(..., min_length=1)
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    stream: bool = False
    stream_options: StreamOptions | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    response_format: dict[str, Any] | None = None
    user: str | None = None  # accepted, ignored
    n: int | None = None  # 400 if n > 1 (RekAI returns a single choice)
    provider: str | None = None  # RekAI extension: explicit provider override


class ChatCompletionMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatCompletionMessage
    finish_reason: Literal["stop", "tool_calls"] = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: Usage  # field names already match OpenAI's
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
    fallback_used: bool = Field(
        default=False, description="True if a fallback served this response, not the primary."
    )
    created: int


class EmbeddingsRequest(BaseModel):
    model: str = Field(..., description="Embedding model, e.g. 'text-embedding-3-small' or 'echo'.")
    input: str | list[str] = Field(..., description="A string or list of strings to embed.")
    provider: str | None = Field(default=None, description="Force a provider (else routed).")
    cache: bool = Field(default=True)


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
    errors_total: int
    fallbacks_total: int
    retries_total: int = 0
    cooldowns_total: int = 0
    tokens_total: int
    cost_usd_total: float
    requests_by_provider: dict[str, int]
    usage_by_client: dict[str, ClientUsage] = Field(
        default_factory=dict,
        description="Per-tenant usage keyed by a masked client id ('key:<hash>' "
        "when gateway auth is on, else the client IP).",
    )


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    providers: list[str]
    provider_status: dict[str, Literal["ready", "byok_only"]] = Field(
        default_factory=dict,
        description="Per-provider readiness: 'ready' (usable now) or "
        "'byok_only' (needs an X-Provider-Key).",
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
