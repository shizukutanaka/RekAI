"""Pydantic request/response models — these define the public OpenAPI schema."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant"]


class ChatMessage(BaseModel):
    role: Role
    content: str = Field(..., min_length=1)


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


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatResponse(BaseModel):
    id: str
    provider: str
    model: str
    content: str
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


class ModelInfo(BaseModel):
    id: str
    provider: str


class ModelsResponse(BaseModel):
    data: list[ModelInfo]


class UsageSummary(BaseModel):
    requests_total: int
    cache_hits_total: int
    cache_misses_total: int
    errors_total: int
    fallbacks_total: int
    tokens_total: int
    cost_usd_total: float
    requests_by_provider: dict[str, int]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    providers: list[str]
    cache: str


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
