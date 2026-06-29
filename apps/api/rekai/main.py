"""FastAPI application factory and route definitions."""

from __future__ import annotations

import json

from fastapi import Depends, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from rekai import __version__
from rekai.cache import CacheBackend, build_cache
from rekai.config import Settings, get_settings
from rekai.logging_config import configure_logging
from rekai.metrics import metrics
from rekai.providers import provider_names
from rekai.providers.base import ProviderError
from rekai.rate_limit import RateLimiter
from rekai.router import select_provider
from rekai.schemas import (
    ChatRequest,
    ChatResponse,
    ErrorResponse,
    HealthResponse,
    ModelInfo,
    ModelsResponse,
)
from rekai.service import handle_chat


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="RekAI",
        version=__version__,
        description="A lightweight AI router & gateway with provider abstraction, "
        "caching and BYOK.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    cache: CacheBackend = build_cache(settings)
    limiter = RateLimiter(settings.rate_limit_requests, settings.rate_limit_window_seconds)

    # --- dependencies -----------------------------------------------------
    def get_cache() -> CacheBackend:
        return cache

    def get_config() -> Settings:
        return settings

    # --- error handling ---------------------------------------------------
    @app.exception_handler(ProviderError)
    async def _provider_error_handler(_: Request, exc: ProviderError) -> JSONResponse:
        metrics.record_error()
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(error="provider_error", detail=str(exc)).model_dump(),
        )

    # --- middleware: rate limiting ---------------------------------------
    @app.middleware("http")
    async def _rate_limit(request: Request, call_next):
        if settings.rate_limit_enabled and request.url.path.startswith("/v1/"):
            client = request.client.host if request.client else "anonymous"
            if not limiter.allow(client):
                metrics.record_error()
                return JSONResponse(
                    status_code=429,
                    content=ErrorResponse(
                        error="rate_limited",
                        detail="Too many requests. Slow down.",
                    ).model_dump(),
                )
        return await call_next(request)

    # --- routes -----------------------------------------------------------
    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            version=__version__,
            providers=provider_names(),
            cache=cache.label,
        )

    @app.get("/metrics", tags=["system"], response_class=PlainTextResponse)
    async def metrics_endpoint() -> str:
        return metrics.render()

    @app.get("/v1/models", response_model=ModelsResponse, tags=["chat"])
    async def list_models() -> ModelsResponse:
        from rekai.providers.registry import get_provider

        data: list[ModelInfo] = []
        for name in provider_names():
            provider = get_provider(name)
            if provider is None:
                continue
            for model in await provider.list_models(None):
                data.append(ModelInfo(id=model, provider=name))
        return ModelsResponse(data=data)

    @app.post(
        "/v1/chat",
        response_model=ChatResponse,
        tags=["chat"],
        responses={
            401: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
        },
    )
    async def chat(
        request: ChatRequest,
        x_provider_key: str | None = Header(default=None, alias="X-Provider-Key"),
        config: Settings = Depends(get_config),
        cache_backend: CacheBackend = Depends(get_cache),
    ) -> ChatResponse:
        return await handle_chat(request, x_provider_key, config, cache_backend)

    @app.post(
        "/v1/chat/stream",
        tags=["chat"],
        responses={
            200: {"content": {"text/event-stream": {}}},
            401: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
        },
    )
    async def chat_stream(
        request: ChatRequest,
        x_provider_key: str | None = Header(default=None, alias="X-Provider-Key"),
        config: Settings = Depends(get_config),
    ) -> StreamingResponse:
        """Stream a chat completion as Server-Sent Events.

        Emits ``data: {"delta": "..."}`` events followed by a terminating
        ``data: [DONE]``. Streaming responses are not cached.
        """
        provider_name, provider = select_provider(request, config)
        metrics.record_request(provider_name)

        async def event_source():
            try:
                async for delta in provider.stream(request, x_provider_key):
                    if delta:
                        yield f"data: {json.dumps({'delta': delta})}\n\n"
            except ProviderError as exc:
                metrics.record_error()
                payload = {"error": "provider_error", "detail": str(exc)}
                yield f"data: {json.dumps(payload)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-RekAI-Provider": provider_name,
            },
        )

    return app


app = create_app()
