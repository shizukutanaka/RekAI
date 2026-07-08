"""FastAPI application factory and route definitions."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Literal, Protocol

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from rekai import __version__, auth, guardrails, idempotency, tracing
from rekai.cache import CacheBackend, build_cache
from rekai.circuit_breaker import consecutive_failures
from rekai.config import Settings, get_settings
from rekai.cooldown import cooldowns
from rekai.keystore import DynamicKeyStore
from rekai.logging_config import configure_logging, get_logger
from rekai.metrics import metrics
from rekai.metrics_store import build_metrics_store
from rekai.pricing import estimate_cost, estimate_tokens, price_for_model
from rekai.providers import get_provider, provider_names
from rekai.providers.base import ProviderError
from rekai.rate_limit import build_rate_limiter
from rekai.router import select_provider
from rekai.schemas import (
    AdminKeyList,
    AdminKeyRequest,
    AdminKeyResponse,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    EmbeddingsRequest,
    EmbeddingsResponse,
    ErrorResponse,
    HealthResponse,
    ModelInfo,
    ModelPricing,
    ModelsResponse,
    ServiceInfo,
    Usage,
    UsageSummary,
)
from rekai.security import KeyCipher, mask_key
from rekai.service import handle_chat, handle_embeddings

access_logger = get_logger("rekai.access")
admin_logger = get_logger("rekai.admin")


def _guardrail_response(
    messages: list[ChatMessage], settings: Settings, response: Response
) -> JSONResponse | None:
    """Run the prompt-injection guardrail. Returns a 403 response when a flagged
    request should be blocked; otherwise sets an X-Guardrail-Flag header (flag
    mode) and returns None so the request proceeds."""
    hit = guardrails.scan_messages(messages, settings.guardrails_enabled)
    if hit is None:
        return None
    if settings.guardrails_action == "block":
        metrics.record_error()
        return JSONResponse(
            status_code=403,
            content=ErrorResponse(
                error="guardrail_blocked",
                detail=f"Request blocked by prompt-injection guardrail ({hit}).",
            ).model_dump(),
        )
    response.headers["X-Guardrail-Flag"] = hit
    return None


def _redact_output(result: ChatResponse, settings: Settings, response: Response) -> ChatResponse:
    """Scrub common secret/API-key patterns from the assistant's content
    (OWASP LLM02). Sets X-Redacted (comma-separated pattern names) when
    anything was redacted; returns ``result`` unchanged otherwise or when
    disabled."""
    if not settings.output_redaction_enabled or not result.content:
        return result
    redacted, hits = guardrails.redact_secrets(result.content)
    if not hits:
        return result
    response.headers["X-Redacted"] = ",".join(hits)
    return result.model_copy(update={"content": redacted})


class _HasUsageAndCost(Protocol):
    @property
    def usage(self) -> Usage: ...
    @property
    def cost_usd(self) -> float | None: ...


def _record_client_usage(
    http_request: Request, result: _HasUsageAndCost, settings: Settings
) -> None:
    """Attribute a chat/embeddings response's tokens and cost to the requesting
    client (the masked API-key id, or the client IP with no gateway auth).

    Counted for every response the client receives — including one served from
    the content cache or replayed via Idempotency-Key — since this tracks what
    the *client* consumed, not RekAI's own upstream spend (that distinction is
    exactly why the cache/idempotency paths don't re-run this on the same
    request; per-client counting intentionally does, once per HTTP call).

    When REKAI_CLIENT_BUDGET_WINDOW_SECONDS is set, this also updates the
    current window's bucket used by the budget-cap check."""
    client_id = getattr(http_request.state, "client_id", None) or "anonymous"
    metrics.record_client_usage(client_id, result.usage.total_tokens, result.cost_usd)
    if settings.client_budget_window_seconds is not None:
        metrics.record_client_budget_usage(
            client_id, result.cost_usd, settings.client_budget_window_seconds, time.time()
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

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
    limiter = build_rate_limiter(
        settings, settings.rate_limit_requests, settings.rate_limit_window_seconds
    )
    admin_limiter = (
        build_rate_limiter(
            settings, settings.admin_rate_limit_requests, settings.admin_rate_limit_window_seconds
        )
        if settings.admin_key
        else None
    )
    key_cipher = (
        KeyCipher(settings.dynamic_keys_encryption_key)
        if settings.dynamic_keys_encryption_key
        else None
    )
    key_store = DynamicKeyStore(cache, key_cipher) if settings.dynamic_keys_enabled else None
    if settings.dynamic_keys_enabled and not settings.cache_enabled:
        access_logger.warning(
            "REKAI_DYNAMIC_KEYS_ENABLED is set but REKAI_CACHE_ENABLED=false, so "
            "added/revoked keys won't persist between requests (NullCache never stores)."
        )

    async def _allowed_keys() -> list[str]:
        """Static REKAI_API_KEYS plus any runtime-added keys, if enabled."""
        if key_store is None:
            return settings.api_key_list
        return settings.api_key_list + await key_store.list_keys()

    # --- dependencies -----------------------------------------------------
    def get_cache() -> CacheBackend:
        return cache

    def get_config() -> Settings:
        return settings

    # --- error handling ---------------------------------------------------
    @app.exception_handler(ProviderError)
    async def _provider_error_handler(_: Request, exc: ProviderError) -> JSONResponse:
        metrics.record_error()
        headers: dict[str, str] = {}
        # Pass an upstream rate-limit's Retry-After through to the client so its
        # SDK can back off by the amount the provider asked for.
        if exc.status_code == 429 and exc.retry_after is not None:
            headers["Retry-After"] = str(int(exc.retry_after))
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(error="provider_error", detail=str(exc)).model_dump(),
            headers=headers or None,
        )

    # --- middleware: auth + body size + rate limiting (the /v1 gate) ------
    @app.middleware("http")
    async def _rate_limit(request: Request, call_next):
        is_api_write = request.method != "OPTIONS" and request.url.path.startswith("/v1/")

        # The rate-limit bucket: the authenticated key (per-tenant) when present,
        # otherwise the client IP. Stashed for the access log.
        rl_client = request.client.host if request.client else "anonymous"
        token: str | None = None

        # Gateway auth: when keys are configured (static or dynamic), /v1/*
        # needs a valid Bearer key. Checked first so unauthenticated traffic
        # can't consume rate budget.
        if is_api_write and (settings.api_key_list or key_store is not None):
            token = auth.parse_bearer(request.headers.get("authorization"))
            if token is None or not auth.key_allowed(token, await _allowed_keys()):
                metrics.record_error()
                return JSONResponse(
                    status_code=401,
                    content=ErrorResponse(
                        error="unauthorized", detail="Missing or invalid API key."
                    ).model_dump(),
                    headers={"WWW-Authenticate": "Bearer"},
                )
            rl_client = auth.client_id(token)
        request.state.client_id = rl_client

        # Per-client spend cap: once exceeded, block before doing any real work
        # (parsing, provider calls) so an over-budget client can't rack up more.
        # A per-key override (client_budgets_usd) wins over the global default.
        budget = settings.client_budget_usd
        if token is not None and token in settings.client_budget_overrides:
            budget = settings.client_budget_overrides[token]
        if is_api_write and budget is not None:
            window = settings.client_budget_window_seconds
            if window is not None:
                spent = metrics.client_budget_window_cost(rl_client, window, time.time())
            else:
                spent = metrics.client_cost_usd(rl_client)
            if spent >= budget:
                metrics.record_error()
                headers = {"X-Budget-Remaining": "0"}
                if window is not None:
                    headers["X-Budget-Reset"] = str((int(time.time() / window) + 1) * window)
                return JSONResponse(
                    status_code=402,
                    content=ErrorResponse(
                        error="budget_exceeded",
                        detail=f"Client budget of ${budget:.2f} exceeded (spent ${spent:.4f}).",
                    ).model_dump(),
                    headers=headers,
                )

        # Reject oversized bodies up front (cheap Content-Length check) so a huge
        # payload can't tie up parsing or memory.
        if is_api_write and settings.max_body_bytes > 0:
            content_length = request.headers.get("content-length")
            if content_length is not None and content_length.isdigit():
                if int(content_length) > settings.max_body_bytes:
                    metrics.record_error()
                    return JSONResponse(
                        status_code=413,
                        content=ErrorResponse(
                            error="payload_too_large",
                            detail=f"Request body exceeds {settings.max_body_bytes} bytes.",
                        ).model_dump(),
                    )

        # CORS preflight (OPTIONS) must not consume budget, or the browser sees a
        # 429 on the preflight ("Failed to fetch") instead of the real response.
        if settings.rate_limit_enabled and is_api_write:
            limit = str(settings.rate_limit_requests)
            if not await limiter.allow(rl_client):
                metrics.record_error()
                retry_after = await limiter.retry_after(rl_client)
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
            response.headers["X-RateLimit-Remaining"] = str(await limiter.remaining(rl_client))
            return response
        return await call_next(request)

    # --- middleware: request id + latency (outermost) --------------------
    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        # W3C trace context: continue an incoming trace or start a new one.
        trace_id = (
            tracing.parse_trace_id(request.headers.get("traceparent")) or tracing.new_trace_id()
        )
        span_id = tracing.new_span_id()
        request.state.trace_id = trace_id
        # Ambient trace id for this request, so a provider's outbound HTTP call
        # (deep in the handler call stack) can attach its own traceparent
        # without threading trace_id through every function signature down to
        # it. Reset unconditionally so it can't leak into an unrelated request.
        trace_token = tracing.set_current_trace_id(trace_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            tracing.reset_current_trace_id(trace_token)
        elapsed_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.1f}"
        response.headers["X-RekAI-Version"] = __version__
        response.headers["traceparent"] = tracing.format_traceparent(trace_id, span_id)
        response.headers["X-Content-Type-Options"] = "nosniff"
        access_logger.info(
            "%s %s -> %s %.1fms id=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            request_id,
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(elapsed_ms, 1),
                "request_id": request_id,
                "trace_id": trace_id,
                "client": getattr(request.state, "client_id", None),
            },
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
            "X-RekAI-Version",
            "Idempotent-Replay",
            "traceparent",
            "X-Guardrail-Flag",
            "X-Redacted",
            "X-Budget-Remaining",
            "X-Budget-Reset",
        ],
    )

    # --- routes -----------------------------------------------------------
    @app.get("/", response_model=ServiceInfo, tags=["system"])
    async def root() -> ServiceInfo:
        return ServiceInfo(
            name=settings.app_name,
            version=__version__,
            description="A lightweight AI router & gateway. See /docs for the API.",
            docs="/docs",
            health="/health",
        )

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
    async def metrics_endpoint(request: Request, response: Response) -> str:
        # /metrics is open by default (so Prometheus can scrape without a
        # token), but it carries a per-client cost/token breakdown once gateway
        # auth is in use — REKAI_METRICS_REQUIRE_AUTH locks it behind the same
        # Bearer key for operators who consider that sensitive.
        if settings.metrics_require_auth and (settings.api_key_list or key_store is not None):
            token = auth.parse_bearer(request.headers.get("authorization"))
            if token is None or not auth.key_allowed(token, await _allowed_keys()):
                metrics.record_error()
                response.status_code = 401
                response.headers["WWW-Authenticate"] = "Bearer"
                return ErrorResponse(
                    error="unauthorized", detail="Missing or invalid API key."
                ).model_dump_json()
        return metrics.render()

    @app.get("/v1/usage", response_model=UsageSummary, tags=["system"])
    async def usage_summary() -> UsageSummary:
        return UsageSummary(**metrics.snapshot())

    # --- admin: runtime key management (only registered when configured) --
    # Deliberately outside /v1/*, so it's governed solely by REKAI_ADMIN_KEY —
    # not the tenant gateway-auth gate above. Every attempt (successful,
    # unauthorized, rate-limited, or not-found) is written to a dedicated
    # audit log (admin_logger) with the masked key and caller IP — this
    # operation has no distinct per-admin identity beyond the shared secret,
    # so IP is the best attribution available.
    if settings.admin_key:
        admin_key = settings.admin_key

        def _admin_ip(request: Request) -> str:
            return request.client.host if request.client else "unknown"

        async def _admin_rate_limited(request: Request) -> JSONResponse | None:
            # Checked *before* the admin-key check (opposite order from the
            # tenant gateway-auth gate above) — there's one shared secret here,
            # not a per-tenant one, so the threat is brute-forcing it, and
            # every attempt (right or wrong key) needs to count toward the
            # budget for that defence to mean anything.
            if not settings.admin_rate_limit_enabled or admin_limiter is None:
                return None
            ip = _admin_ip(request)
            if await admin_limiter.allow(ip):
                return None
            retry_after = await admin_limiter.retry_after(ip)
            admin_logger.warning(
                "admin rate limited ip=%s",
                ip,
                extra={"admin_action": "rate_limited", "ip": ip},
            )
            return JSONResponse(
                status_code=429,
                content=ErrorResponse(
                    error="rate_limited",
                    detail=f"Too many admin requests. Retry in {retry_after}s.",
                ).model_dump(),
                headers={"Retry-After": str(retry_after)},
            )

        def _admin_authorized(request: Request) -> bool:
            token = auth.parse_bearer(request.headers.get("authorization"))
            ok = token is not None and auth.key_allowed(token, [admin_key])
            if not ok:
                admin_logger.warning(
                    "admin auth failed method=%s path=%s ip=%s",
                    request.method,
                    request.url.path,
                    _admin_ip(request),
                    extra={
                        "admin_action": "auth_failed",
                        "method": request.method,
                        "path": request.url.path,
                        "ip": _admin_ip(request),
                    },
                )
            return ok

        def _admin_auth_error() -> JSONResponse:
            return JSONResponse(
                status_code=401,
                content=ErrorResponse(
                    error="unauthorized", detail="Missing or invalid admin key."
                ).model_dump(),
                headers={"WWW-Authenticate": "Bearer"},
            )

        def _dynamic_keys_disabled_error() -> JSONResponse:
            return JSONResponse(
                status_code=400,
                content=ErrorResponse(
                    error="dynamic_keys_disabled",
                    detail="Set REKAI_DYNAMIC_KEYS_ENABLED=true to manage keys at runtime.",
                ).model_dump(),
            )

        @app.get("/admin/keys", response_model=AdminKeyList, tags=["admin"])
        async def list_admin_keys(request: Request):
            rate_limited = await _admin_rate_limited(request)
            if rate_limited is not None:
                return rate_limited
            if not _admin_authorized(request):
                return _admin_auth_error()
            dynamic = await key_store.list_keys() if key_store is not None else []
            admin_logger.info(
                "admin listed keys ip=%s",
                _admin_ip(request),
                extra={"admin_action": "list_keys", "ip": _admin_ip(request)},
            )
            return AdminKeyList(
                static=[mask_key(k) for k in settings.api_key_list],
                dynamic=[mask_key(k) for k in dynamic],
            )

        @app.post("/admin/keys", response_model=AdminKeyResponse, tags=["admin"], status_code=201)
        async def add_admin_key(payload: AdminKeyRequest, request: Request):
            rate_limited = await _admin_rate_limited(request)
            if rate_limited is not None:
                return rate_limited
            if not _admin_authorized(request):
                return _admin_auth_error()
            if key_store is None:
                return _dynamic_keys_disabled_error()
            await key_store.add(payload.key)
            masked = mask_key(payload.key)
            admin_logger.info(
                "admin added key=%s ip=%s",
                masked,
                _admin_ip(request),
                extra={"admin_action": "add_key", "key": masked, "ip": _admin_ip(request)},
            )
            return AdminKeyResponse(status="added", key=masked)

        @app.delete("/admin/keys/{key}", response_model=AdminKeyResponse, tags=["admin"])
        async def revoke_admin_key(key: str, request: Request):
            rate_limited = await _admin_rate_limited(request)
            if rate_limited is not None:
                return rate_limited
            if not _admin_authorized(request):
                return _admin_auth_error()
            if key_store is None:
                return _dynamic_keys_disabled_error()
            removed = await key_store.revoke(key)
            masked = mask_key(key)
            if not removed:
                admin_logger.warning(
                    "admin revoke failed (not found) key=%s ip=%s",
                    masked,
                    _admin_ip(request),
                    extra={
                        "admin_action": "revoke_key_not_found",
                        "key": masked,
                        "ip": _admin_ip(request),
                    },
                )
                return JSONResponse(
                    status_code=404,
                    content=ErrorResponse(
                        error="not_found",
                        detail="Key not found among dynamically-added keys.",
                    ).model_dump(),
                )
            admin_logger.info(
                "admin revoked key=%s ip=%s",
                masked,
                _admin_ip(request),
                extra={"admin_action": "revoke_key", "key": masked, "ip": _admin_ip(request)},
            )
            return AdminKeyResponse(status="revoked", key=masked)

    @app.get("/v1/models", response_model=ModelsResponse, tags=["chat"])
    async def list_models(
        type: Literal["chat", "embedding"] | None = Query(
            None, description="Filter by model type: 'chat' or 'embedding'."
        ),
    ) -> ModelsResponse:
        def _info(model: str, name: str, kind: Literal["chat", "embedding"]) -> ModelInfo:
            price = price_for_model(model, settings.pricing_override_dict)
            pricing = (
                ModelPricing(input_per_1m=price[0], output_per_1m=price[1])
                if price is not None
                else None
            )
            return ModelInfo(id=model, provider=name, type=kind, pricing=pricing)

        data: list[ModelInfo] = []
        for name in provider_names():
            provider = get_provider(name)
            if provider is None:
                continue
            if type != "embedding":
                for model in await provider.list_models(None):
                    data.append(_info(model, name, "chat"))
            if type != "chat":
                for model in await provider.list_embedding_models(None):
                    data.append(_info(model, name, "embedding"))
        return ModelsResponse(data=data)

    @app.post(
        "/v1/chat",
        response_model=ChatResponse,
        tags=["chat"],
        responses={
            401: {"model": ErrorResponse},
            403: {"model": ErrorResponse},
            413: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
        },
    )
    async def chat(
        response: Response,
        request: ChatRequest,
        http_request: Request,
        x_provider_key: str | None = Header(default=None, alias="X-Provider-Key"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        config: Settings = Depends(get_config),
        cache_backend: CacheBackend = Depends(get_cache),
    ):
        blocked = _guardrail_response(request.messages, config, response)
        if blocked is not None:
            return blocked
        if idempotency_key:
            stored = await idempotency.get(cache_backend, idempotency_key)
            if stored is not None:
                response.headers["Idempotent-Replay"] = "true"
                replayed = ChatResponse(**stored)
                _record_client_usage(http_request, replayed, config)
                return replayed
        result = await handle_chat(request, x_provider_key, config, cache_backend)
        result = _redact_output(result, config, response)
        _record_client_usage(http_request, result, config)
        if idempotency_key:
            await idempotency.store(
                cache_backend,
                idempotency_key,
                result.model_dump_json(),
                config.idempotency_ttl_seconds,
            )
        return result

    @app.post(
        "/v1/embeddings",
        response_model=EmbeddingsResponse,
        tags=["embeddings"],
        responses={
            400: {"model": ErrorResponse},
            401: {"model": ErrorResponse},
            413: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
        },
    )
    async def embeddings(
        response: Response,
        request: EmbeddingsRequest,
        http_request: Request,
        x_provider_key: str | None = Header(default=None, alias="X-Provider-Key"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        config: Settings = Depends(get_config),
        cache_backend: CacheBackend = Depends(get_cache),
    ) -> EmbeddingsResponse:
        if idempotency_key:
            stored = await idempotency.get(cache_backend, idempotency_key)
            if stored is not None:
                response.headers["Idempotent-Replay"] = "true"
                replayed = EmbeddingsResponse(**stored)
                _record_client_usage(http_request, replayed, config)
                return replayed
        result = await handle_embeddings(request, x_provider_key, config, cache_backend)
        _record_client_usage(http_request, result, config)
        if idempotency_key:
            await idempotency.store(
                cache_backend,
                idempotency_key,
                result.model_dump_json(),
                config.idempotency_ttl_seconds,
            )
        return result

    @app.post(
        "/v1/chat/stream",
        tags=["chat"],
        responses={
            200: {"content": {"text/event-stream": {}}},
            401: {"model": ErrorResponse},
            403: {"model": ErrorResponse},
            413: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
        },
    )
    async def chat_stream(
        response: Response,
        request: ChatRequest,
        http_request: Request,
        x_provider_key: str | None = Header(default=None, alias="X-Provider-Key"),
        config: Settings = Depends(get_config),
        cache_backend: CacheBackend = Depends(get_cache),
    ):
        """Stream a chat completion as Server-Sent Events.

        Emits ``data: {"delta": "..."}`` events, then a final
        ``data: {"usage": {...}, "cost_usd": ..., "estimated": true}`` summary,
        then a terminating ``data: [DONE]``. Streaming responses are not cached.
        """
        blocked = _guardrail_response(request.messages, config, response)
        if blocked is not None:
            return blocked
        guardrail_flag = response.headers.get("X-Guardrail-Flag")
        client_id = getattr(http_request.state, "client_id", None) or "anonymous"
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
                # Park the provider consistent with the non-streaming path, so
                # later requests — including on other workers/nodes, and other
                # non-streaming requests checking cooldown — route around it.
                # There's no fallback chain here (single provider, no rerouting
                # within this request), so this only benefits subsequent calls.
                if config.provider_cooldown_enabled and exc.status_code == 429:
                    await cooldowns.mark_shared(
                        cache_backend,
                        provider_name,
                        exc.retry_after
                        if exc.retry_after is not None
                        else config.provider_cooldown_seconds,
                    )
                    metrics.record_cooldown()
                elif config.provider_cooldown_enabled and exc.status_code >= 500:
                    failures = consecutive_failures.record_failure(provider_name)
                    if failures >= config.circuit_breaker_threshold:
                        await cooldowns.mark_shared(
                            cache_backend, provider_name, config.provider_cooldown_seconds
                        )
                        metrics.record_cooldown()
                payload = {"error": "provider_error", "detail": str(exc)}
                yield f"data: {json.dumps(payload)}\n\n"

            if not errored:
                consecutive_failures.record_success(provider_name)
                # Prefer provider-reported usage; otherwise estimate from text.
                estimated = reported_usage is None
                if reported_usage is not None:
                    usage = reported_usage
                else:
                    prompt_tokens = sum(estimate_tokens(m.content or "") for m in request.messages)
                    completion_tokens = estimate_tokens("".join(completion))
                    usage = Usage(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=prompt_tokens + completion_tokens,
                    )
                cost_usd = estimate_cost(
                    provider_name, request.model, usage, config.pricing_override_dict
                )
                metrics.record_tokens(usage.total_tokens)
                metrics.record_cost(cost_usd)
                metrics.record_client_usage(client_id, usage.total_tokens, cost_usd)
                if config.client_budget_window_seconds is not None:
                    metrics.record_client_budget_usage(
                        client_id, cost_usd, config.client_budget_window_seconds, time.time()
                    )
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

        stream_headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-RekAI-Provider": provider_name,
        }
        if guardrail_flag:
            stream_headers["X-Guardrail-Flag"] = guardrail_flag
        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers=stream_headers,
        )

    return app


app = create_app()
