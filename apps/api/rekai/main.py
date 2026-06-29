"""FastAPI application factory and route definitions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from rekai import __version__
from rekai.cache import CacheBackend, build_cache
from rekai.config import Settings, get_settings
from rekai.logging_config import configure_logging, get_logger
from rekai.metrics import metrics
from rekai.metrics_store import build_metrics_store
from rekai.pricing import estimate_cost, estimate_tokens
from rekai.providers import get_provider, provider_names
from rekai.providers.base import ProviderError
from rekai.rate_limit import RateLimiter
from rekai.router import select_provider
from rekai.schemas import (
    ChatRequest,
    ChatResponse,
    EmbeddingsRequest,
    EmbeddingsResponse,
    ErrorResponse,
    HealthResponse,
    ModelInfo,
    ModelsResponse,
    Usage,
    UsageSummary,
)
from rekai.service import handle_chat, handle_embeddings

access_logger = get_logger("rekai.access")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    metrics_store = build_metrics_store(settings)

    async def _flush_loop(interval: int) -> None:
        while True:
            await asyncio.sleep(interval)
            await metrics_store.save(metrics.snapshot())

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        baseline = await metrics_store.load()
        if baseline:
            metrics.seed(baseline)
            access_logger.info("loaded persisted metrics snapshot")
        flush_task = asyncio.create_task(_flush_loop(settings.metrics_persist_interval_seconds))
        try:
            yield
        finally:
            flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flush_task
            await metrics_store.save(metrics.snapshot())

    app = FastAPI(
        title="RekAI",
        version=__version__,
        description="A lightweight AI router & gateway with provider abstraction, "
        "caching and BYOK.",
        lifespan=lifespan,
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
        # CORS preflight (OPTIONS) must not consume budget, or the browser sees a
        # 429 on the preflight ("Failed to fetch") instead of the real response.
        if (
            settings.rate_limit_enabled
            and request.method != "OPTIONS"
            and request.url.path.startswith("/v1/")
        ):
            client = request.client.host if request.client else "anonymous"
            limit = str(settings.rate_limit_requests)
            if not limiter.allow(client):
                metrics.record_error()
                retry_after = limiter.retry_after(client)
                return JSONResponse(
                    status_code=429,
                    content=ErrorResponse(
                        error="rate_limited",
                        detail=f"Too many requests. Retry in {retry_after}s.",
                    ).model_dump(),
                    headers={
                        "Retry-After": str(retry_after),
                        "X-RateLimit-Limit": limit,
                        "X-RateLimit-Remaining": "0",
                    },
                )
            response = await call_next(request)
            response.headers["X-RateLimit-Limit"] = limit
            response.headers["X-RateLimit-Remaining"] = str(limiter.remaining(client))
            return response
        return await call_next(request)

    # --- middleware: request id + latency (outermost) --------------------
    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.1f}"
        access_logger.info(
            "%s %s -> %s %.1fms id=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            request_id,
        )
        return response

    # CORS is added last so it wraps the others (outermost): short-circuit
    # responses like a 429 from the rate limiter still get CORS headers, so the
    # browser can read them instead of failing the fetch.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
        # Expose custom response headers so browser JS can read them (they are
        # not CORS-safelisted by default).
        expose_headers=[
            "Retry-After",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-Request-ID",
            "X-Response-Time-Ms",
        ],
    )

    # --- routes -----------------------------------------------------------
    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        provider_status: dict[str, Literal["ready", "byok_only"]] = {}
        for name in provider_names():
            provider = get_provider(name)
            ready = provider is not None and provider.server_key_configured()
            provider_status[name] = "ready" if ready else "byok_only"
        return HealthResponse(
            status="ok",
            version=__version__,
            providers=provider_names(),
            provider_status=provider_status,
            cache=cache.label,
        )

    @app.get("/metrics", tags=["system"], response_class=PlainTextResponse)
    async def metrics_endpoint() -> str:
        return metrics.render()

    @app.get("/v1/usage", response_model=UsageSummary, tags=["system"])
    async def usage_summary() -> UsageSummary:
        return UsageSummary(**metrics.snapshot())

    @app.get("/v1/models", response_model=ModelsResponse, tags=["chat"])
    async def list_models(
        type: Literal["chat", "embedding"] | None = Query(
            None, description="Filter by model type: 'chat' or 'embedding'."
        ),
    ) -> ModelsResponse:
        data: list[ModelInfo] = []
        for name in provider_names():
            provider = get_provider(name)
            if provider is None:
                continue
            if type != "embedding":
                for model in await provider.list_models(None):
                    data.append(ModelInfo(id=model, provider=name, type="chat"))
            if type != "chat":
                for model in await provider.list_embedding_models(None):
                    data.append(ModelInfo(id=model, provider=name, type="embedding"))
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
        "/v1/embeddings",
        response_model=EmbeddingsResponse,
        tags=["embeddings"],
        responses={
            400: {"model": ErrorResponse},
            401: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
        },
    )
    async def embeddings(
        request: EmbeddingsRequest,
        x_provider_key: str | None = Header(default=None, alias="X-Provider-Key"),
        config: Settings = Depends(get_config),
        cache_backend: CacheBackend = Depends(get_cache),
    ) -> EmbeddingsResponse:
        return await handle_embeddings(request, x_provider_key, config, cache_backend)

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

        Emits ``data: {"delta": "..."}`` events, then a final
        ``data: {"usage": {...}, "cost_usd": ..., "estimated": true}`` summary,
        then a terminating ``data: [DONE]``. Streaming responses are not cached.
        """
        provider_name, provider = select_provider(request, config)
        metrics.record_request(provider_name)

        async def event_source():
            completion = []
            reported_usage: Usage | None = None
            reported_tool_calls: list[dict] | None = None
            errored = False
            try:
                async for event in provider.stream_events(request, x_provider_key):
                    if event.delta:
                        completion.append(event.delta)
                        yield f"data: {json.dumps({'delta': event.delta})}\n\n"
                    if event.usage is not None:
                        reported_usage = event.usage
                    if event.tool_calls is not None:
                        reported_tool_calls = event.tool_calls
            except ProviderError as exc:
                errored = True
                metrics.record_error()
                payload = {"error": "provider_error", "detail": str(exc)}
                yield f"data: {json.dumps(payload)}\n\n"

            if not errored:
                # Prefer provider-reported usage; otherwise estimate from text.
                estimated = reported_usage is None
                if reported_usage is not None:
                    usage = reported_usage
                else:
                    prompt_tokens = sum(estimate_tokens(m.content) for m in request.messages)
                    completion_tokens = estimate_tokens("".join(completion))
                    usage = Usage(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=prompt_tokens + completion_tokens,
                    )
                cost_usd = estimate_cost(provider_name, request.model, usage)
                metrics.record_tokens(usage.total_tokens)
                metrics.record_cost(cost_usd)
                summary = {
                    "provider": provider_name,
                    "model": request.model,
                    "usage": usage.model_dump(),
                    "cost_usd": cost_usd,
                    "estimated": estimated,
                }
                if reported_tool_calls:
                    summary["tool_calls"] = reported_tool_calls
                yield f"data: {json.dumps(summary)}\n\n"
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
