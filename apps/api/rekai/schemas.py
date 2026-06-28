"""Pydantic request/response models — these define the public OpenAPI schema."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant"]


class ChatMessage(BaseModel):
    role: Role
    content: str = Field(..., min_length=1)


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
    cached: bool = False
    created: int


class ModelInfo(BaseModel):
    id: str
    provider: str


class ModelsResponse(BaseModel):
    data: list[ModelInfo]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    providers: list[str]
    cache: str


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
