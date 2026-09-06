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
from pydantic import ValidationError

from rekai import __version__, auth, guardrails, idempotency, openai_compat, tracing
from rekai.cache import CacheBackend, build_cache
from rekai.config import Settings, get_settings
from rekai.cooldown import cooldowns
from rekai.keystore import DynamicKeyStore
from rekai.logging_config import configure_logging, get_logger
from rekai.metrics import merge_snapshots, metrics
from rekai.metrics_store import build_metrics_store
from rekai.pricing import price_for_model
from rekai.providers import get_provider, provider_names
from rekai.providers.base import ProviderError
from rekai.rate_limit import build_rate_limiter
from rekai.router import resolve_provider, select_provider
from rekai.schemas import (
    AdminKeyList,
    AdminKeyRequest,
    AdminKeyResponse,
    ChatCompletionsRequest,
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
from rekai.semantic_cache import semantic_cache
from rekai.service import handle_chat, handle_chat_stream, handle_embeddings

access_logger = get_logger("rekai.access")
admin_logger = get_logger("rekai.admin")


class MaxBodySizeMiddleware:
    """Pure-ASGI middleware enforcing a hard cap on /v1/* request bodies.

    The Content-Length pre-check in ``_rate_limit`` below is only advisory — a
    client using chunked transfer-encoding sends no Content-Length at all, so
    FastAPI would otherwise buffer the whole body (however large) before any
    validation runs. This buffers incoming chunks up to (and one chunk past)
    the limit and, the moment the running total exceeds it, sends a 413
    directly and never invokes the downstream app — bodies within the limit
    are replayed to the app via a synthetic ``receive`` unchanged.

    This has to be a plain ASGI middleware, not a `@app.middleware("http")`/
    `BaseHTTPMiddleware` dispatch function: raising an exception while a
    *downstream* `request.body()` call is in flight gets wrapped in an anyio
    `ExceptionGroup` by `BaseHTTPMiddleware`'s internal receive-forwarding
    (confirmed empirically), which loses its type before FastAPI's own
    body-parsing code can recognize it as an `HTTPException` — so it falls
    through to FastAPI's generic "there was an error parsing the body" 400,
    not the intended 413. Rejecting before the app is ever invoked sidesteps
    that translation entirely.
    """

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope["type"] != "http"
            or self.max_bytes <= 0
            or not scope["path"].startswith("/v1/")
            or scope["method"] == "OPTIONS"
        ):
            await self.app(scope, receive, send)
            return

        chunks: list[dict] = []
        total = 0
        while True:
            message = await receive()
            chunks.append(message)
            if message["type"] == "http.disconnect":
                break
            total += len(message.get("body", b""))
            if total > self.max_bytes:
                # The advisory Content-Length check in _rate_limit counts its
                # rejections; this hard cap — the one a chunked upload actually
                # trips — recorded nothing, so the rejections that mattered most
                # were invisible in both the metrics and the access log.
                metrics.record_error("payload_too_large")
                response = JSONResponse(
                    status_code=413,
                    content=ErrorResponse(
                        error="payload_too_large",
                        detail=f"Request body exceeds {self.max_bytes} bytes.",
                    ).model_dump(),
                )
                await response(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        index = 0

        async def replay_receive():
            nonlocal index
            if index < len(chunks):
                message = chunks[index]
                index += 1
                return message
            # Once the buffered body is replayed, hand off to the real
            # receive so a later disconnect check (e.g. StreamingResponse
            # watching for the client going away mid-SSE-stream) gets an
            # honest answer instead of a fabricated immediate disconnect,
            # which would otherwise look like the client vanished and cut
            # a streaming response short right after it starts.
            return await receive()

        await self.app(scope, replay_receive, send)


class ConcurrencyLimitMiddleware:
    """Cap the number of `/v1/*` requests in flight at once.

    The rate limiter counts **arrivals** — it says nothing about how many
    requests are still running. Those are different quantities for an LLM
    gateway, where one request can occupy a worker for minutes: 60 requests/min
    is satisfiable by 60 concurrent 60-second streams. Worse, `httpx`'s read
    timeout resets on every chunk, so a slow-trickling upstream can hold a
    streaming request open indefinitely without ever tripping
    `REKAI_REQUEST_TIMEOUT_SECONDS`. Nothing else in the stack bounds occupancy.

    Excess requests are **rejected** with 429 + `Retry-After`, not queued —
    queueing an LLM request behind a minutes-long one just converts a fast
    failure the client can act on into a timeout it can't.

    Pure ASGI rather than a `@app.middleware("http")` dispatch for the reason
    that matters here: `BaseHTTPMiddleware` returns from `call_next` as soon as
    the response *starts*, so a slot released there would be released before a
    streamed body had sent a single token — exactly the case this exists for.
    Wrapping the app means the slot is held until the last byte is sent.

    Process-local, like the default rate limiter: with N uvicorn workers the
    effective cap is N × the configured value.
    """

    def __init__(self, app, max_concurrent: int) -> None:
        self.app = app
        self.max_concurrent = max_concurrent
        self._in_flight = 0

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope["type"] != "http"
            or self.max_concurrent <= 0
            or not scope["path"].startswith("/v1/")
            or scope["method"] == "OPTIONS"
        ):
            await self.app(scope, receive, send)
            return

        # Atomic with respect to the event loop: no await between the read and
        # the increment, so two coroutines can't both see a free slot (the same
        # property MemoryCache.add relies on).
        if self._in_flight >= self.max_concurrent:
            metrics.record_error("concurrency_limit")
            response = JSONResponse(
                status_code=429,
                content=ErrorResponse(
                    error="concurrency_limit",
                    detail=f"Too many requests in flight (limit {self.max_concurrent}).",
                ).model_dump(),
                headers={"Retry-After": "1"},
            )
            await response(scope, receive, send)
            return

        self._in_flight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self._in_flight -= 1


# Paths an OpenAI SDK talks to that RekAI promises to serve *as OpenAI*. Only
# /v1/chat/completions is that promise (README: "a drop-in POST
# /v1/chat/completions"); /v1/chat, /v1/embeddings and /v1/usage are RekAI's own
# API, and the three first-party clients read `detail || error` off their error
# bodies, so their shape must not change.
_OPENAI_COMPAT_PATHS = frozenset({"/v1/chat/completions"})


def _validation_message(detail: list) -> tuple[str, str | None]:
    """Render FastAPI's list-of-dicts validation detail as a message + param.

    OpenAI reports a bad request as prose ("Missing required parameter:
    'messages'"), not as a machine-readable list, and an SDK caller reads
    ``exc.message`` / ``exc.param``. Returns the param only when a single field
    is at fault, since OpenAI's envelope has room for exactly one.
    """
    parts: list[str] = []
    params: list[str] = []
    for err in detail:
        if not isinstance(err, dict):
            continue
        # loc[0] is the source ("body"), which is noise to the caller.
        loc = ".".join(str(p) for p in err.get("loc", ())[1:]) or "body"
        params.append(loc)
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    if not parts:
        return "Invalid request.", None
    return "; ".join(parts), params[0] if len(params) == 1 else None


def _openai_error_body(raw: bytes, status_code: int) -> bytes:
    """Translate whatever error body the stack produced into OpenAI's envelope.

    Passes the body through untouched when it is not a JSON object, or when it
    is already an envelope (``error`` is a mapping), so this is idempotent and
    safe to run over a response the route wrote itself.
    """
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return raw
    if not isinstance(payload, dict):
        return raw
    error = payload.get("error")
    if isinstance(error, dict):
        return raw

    param: str | None = None
    detail = payload.get("detail")
    if isinstance(detail, list):
        # FastAPI's RequestValidationError.
        message, param = _validation_message(detail)
    elif isinstance(detail, str):
        message = detail
    elif isinstance(error, str):
        message = error
    else:
        return raw
    envelope = openai_compat.openai_error(status_code, message, param=param)
    return json.dumps(envelope).encode()


class OpenAICompatErrorMiddleware:
    """Give *every* error on the OpenAI-compatible endpoint OpenAI's envelope.

    `openai_compat.openai_error` existed already, but it was applied by hand
    inside the route function, which can only cover errors the route itself
    produces. Everything else on that path escaped it: auth (401), the client
    budget (402), the body cap (413), the rate limiter and concurrency cap
    (429), and FastAPI's own request validation (422) all answered with RekAI's
    flat `{"error": str, "detail": str}` — a shape the OpenAI SDK cannot read,
    leaving `exc.body` a bare string and `exc.code`/`.type` empty.

    Owning the translation in one place instead lets the route stop knowing
    about the envelope at all, which is what fixes the rest: with its
    hand-written `except ProviderError` gone, an upstream error reaches
    `_provider_error_handler` again and so is both counted in metrics and
    allowed to keep its `Retry-After`.

    Pure ASGI, and installed outside the middlewares above, because that is the
    only position from which their short-circuit responses are visible. Only
    bodies of error responses are buffered; a 200 (including a streamed one) is
    forwarded chunk by chunk, untouched.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"] not in _OPENAI_COMPAT_PATHS:
            await self.app(scope, receive, send)
            return

        start: dict | None = None
        chunks: list[bytes] = []

        async def _send(message: dict) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                if message["status"] >= 400:
                    # Held back: rewriting the body changes Content-Length.
                    start = message
                    return
                await send(message)
                return
            if start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = _openai_error_body(b"".join(chunks), start["status"])
            headers = [(k, v) for k, v in start["headers"] if k.lower() != b"content-length"]
            headers.append((b"content-length", str(len(body)).encode()))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, _send)


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
        metrics.record_error("guardrail_blocked")
        return JSONResponse(
            status_code=403,
            content=ErrorResponse(
                error="guardrail_blocked",
                detail=f"Request blocked by prompt-injection guardrail ({hit}).",
            ).model_dump(),
        )
    response.headers["X-Guardrail-Flag"] = hit
    return None


def _idempotency_error(status_code: int, detail: str) -> JSONResponse:
    """A 409/422 for an Idempotency-Key that conflicts with an existing record."""
    metrics.record_error("idempotency_error")
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(error="idempotency_error", detail=detail).model_dump(),
    )


_IDEM_MISMATCH = "Idempotency-Key was already used with a different request body."
_IDEM_CONFLICT = "A request with this Idempotency-Key is already being processed."


def _client_id(http_request: Request) -> str:
    """The requesting tenant: the masked API-key id under gateway auth, else the
    client IP (set by the ``_rate_limit`` middleware)."""
    return getattr(http_request.state, "client_id", None) or "anonymous"


def _redact_output(result: ChatResponse, settings: Settings, response: Response) -> ChatResponse:
    """Surface output redaction (OWASP LLM02) as the X-Redacted header.

    The scrubbing itself happens in ``rekai.service._redact``, before the
    response is written to any cache or the idempotency store, so ``result``
    normally arrives already clean with ``result.redacted`` naming what was
    caught — including on a cache hit or an idempotent replay, which is why the
    header survives those paths.

    The re-scan below is a backstop for content that was stored *before*
    redaction was switched on (an entry cached while the setting was off, whose
    TTL outlived the config change). It is a no-op on already-scrubbed text."""
    hits = list(result.redacted or [])
    if settings.output_redaction_enabled and result.content:
        scrubbed, late_hits = guardrails.redact_secrets(result.content)
        if late_hits:
            result = result.model_copy(update={"content": scrubbed})
            hits += [h for h in late_hits if h not in hits]
    if hits:
        response.headers["X-Redacted"] = ",".join(hits)
    return result


class _HasUsageAndCost(Protocol):
    @property
    def provider(self) -> str: ...
    @property
    def model(self) -> str: ...
    @property
    def usage(self) -> Usage: ...
    @property
    def cost_usd(self) -> float | None: ...


def _stash_gen_ai(http_request: Request, operation: str, result: _HasUsageAndCost) -> None:
    """Attach OpenTelemetry GenAI semantic-convention attributes to the request
    so the access-log line carries them (picked up in ``_request_context``).

    These are the standard names GenAI observability tools (Datadog, Grafana,
    …) key on, so RekAI's structured logs drop straight into a GenAI dashboard
    without a full OTel SDK integration."""
    http_request.state.gen_ai = {
        "gen_ai.operation.name": operation,
        "gen_ai.provider.name": result.provider,
        "gen_ai.request.model": result.model,
        "gen_ai.usage.input_tokens": result.usage.prompt_tokens,
        "gen_ai.usage.output_tokens": result.usage.completion_tokens,
    }


def _stash_gen_ai_prestream(http_request: Request, provider: str, model: str) -> None:
    """The model/provider GenAI attributes for a streaming request, set before
    the body streams. Token usage isn't known yet (the access-log line fires
    before the stream is consumed), so it's deliberately omitted."""
    http_request.state.gen_ai = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": model,
    }


def _record_client_usage(
    http_request: Request,
    result: _HasUsageAndCost,
    settings: Settings,
    operation: str = "chat",
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
    client_id = _client_id(http_request)
    metrics.record_client_usage(client_id, result.usage.total_tokens, result.cost_usd)
    if settings.client_budget_window_seconds is not None:
        metrics.record_client_budget_usage(
            client_id, result.cost_usd, settings.client_budget_window_seconds, time.time()
        )
    _stash_gen_ai(http_request, operation, result)


async def _run_chat(
    request: ChatRequest,
    http_request: Request,
    response: Response,
    x_provider_key: str | None,
    idempotency_key: str | None,
    settings: Settings,
    cache_backend: CacheBackend,
) -> ChatResponse | JSONResponse:
    """The shared non-streaming chat pipeline.

    Guardrail check, Idempotency-Key replay, provider call (routing/cache/retry/
    fallback via ``handle_chat``), output redaction, per-client accounting, then
    idempotency store. Shared by POST /v1/chat and the OpenAI-compatible POST
    /v1/chat/completions so neither duplicates the flow.

    Returns a ``ChatResponse`` normally, or a ``JSONResponse`` for **any** of
    three refusals: a guardrail block (403), an ``Idempotency-Key`` reused with
    a different body (422), and one already in flight (409). Callers must not
    assume which — this docstring used to say "when the guardrail blocks the
    request", and the OpenAI-compatible route believed it and reported every
    idempotency conflict as a prompt-injection block."""
    blocked = _guardrail_response(request.messages, settings, response)
    if blocked is not None:
        return blocked
    fingerprint: str | None = None
    claimed = False
    client_id = _client_id(http_request)
    if idempotency_key:
        fingerprint = idempotency.fingerprint(request.model_dump_json())
        outcome = await idempotency.claim(
            cache_backend,
            client_id,
            idempotency_key,
            fingerprint,
            settings.idempotency_ttl_seconds,
        )
        if outcome.kind == "mismatch":
            return _idempotency_error(422, _IDEM_MISMATCH)
        if outcome.kind == "conflict":
            return _idempotency_error(409, _IDEM_CONFLICT)
        if outcome.kind == "replay" and outcome.response is not None:
            response.headers["Idempotent-Replay"] = "true"
            replayed = _redact_output(ChatResponse(**outcome.response), settings, response)
            _record_client_usage(http_request, replayed, settings)
            return replayed
        claimed = True  # we hold the in-progress sentinel
    try:
        result = await handle_chat(request, x_provider_key, settings, cache_backend, client_id)
    except Exception:
        # Free the sentinel so the client can retry immediately instead of
        # getting a 409 until it expires.
        if claimed:
            await idempotency.release(cache_backend, client_id, idempotency_key)  # type: ignore[arg-type]
        raise
    result = _redact_output(result, settings, response)
    if result.cache_similarity is not None:
        # Disclose that this answer is to a *similar* prompt, not this one —
        # otherwise a semantic hit is indistinguishable from an exact one.
        response.headers["X-Cache-Similarity"] = f"{result.cache_similarity:.4f}"
    _record_client_usage(http_request, result, settings)
    if idempotency_key and fingerprint is not None:
        await idempotency.complete(
            cache_backend,
            client_id,
            idempotency_key,
            fingerprint,
            result.model_dump(mode="json"),
            settings.idempotency_ttl_seconds,
        )
    return result


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)
    # The metrics and semantic-cache singletons predate any Settings instance;
    # apply their per-deployment bounds before either can serve a request.
    metrics.max_tracked_clients = settings.max_tracked_clients
    semantic_cache.resize(settings.semantic_cache_max_entries)

    if settings.semantic_cache_enabled:
        if not settings.semantic_cache_model:
            raise ValueError(
                "REKAI_SEMANTIC_CACHE_ENABLED=true requires REKAI_SEMANTIC_CACHE_MODEL. "
                "A semantic hit answers a prompt that was never sent, so it needs a real "
                "embeddings model (e.g. text-embedding-3-small); there is no safe default."
            )
        if resolve_provider(None, settings.semantic_cache_model, settings) == "echo":
            access_logger.warning(
                "REKAI_SEMANTIC_CACHE_MODEL=%s resolves to the echo provider, whose "
                "embeddings are a 16-dimension hash, not semantic. Unrelated prompts sit "
                "near 0.78 cosine, so a large share of pairs clear the default 0.85 "
                "threshold and unrelated questions will be answered from cache. Use this "
                "for tests only.",
                settings.semantic_cache_model,
            )

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
    # REKAI_ENVIRONMENT used to be decoration — declared, documented, set by
    # compose and every test, and read by nothing. This is the one thing knowing
    # you are in production is for: refuse to serve as an open proxy for the
    # operator's own provider keys. Outside production it is a loud warning, so
    # the default `development` deployment is not broken by an upgrade.
    hazard = settings.open_proxy_hazard()
    if hazard is not None:
        if settings.environment == "production":
            raise RuntimeError(f"Refusing to start: {hazard}")
        access_logger.warning("%s (REKAI_ENVIRONMENT=production refuses to start.)", hazard)

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
        metrics.record_error("provider_error")
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
                metrics.record_error("unauthorized")
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
                metrics.record_error("budget_exceeded")
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
        # payload can't tie up parsing or memory. This is only advisory — a
        # client using chunked transfer-encoding sends no Content-Length at
        # all, or the header could simply understate the real size — the hard
        # cap enforced against every byte actually received is
        # MaxBodySizeMiddleware, wrapped around the whole app (see create_app).
        if is_api_write and settings.max_body_bytes > 0:
            content_length = request.headers.get("content-length")
            if content_length is not None and content_length.isdigit():
                if int(content_length) > settings.max_body_bytes:
                    metrics.record_error("payload_too_large")
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
                metrics.record_error("rate_limited")
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
        tracestate = tracing.parse_tracestate(request.headers.get("tracestate"))
        trace_token = tracing.set_current_trace_id(trace_id)
        tracestate_token = tracing.set_current_tracestate(tracestate)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            tracing.reset_current_trace_id(trace_token)
            tracing.reset_current_tracestate(tracestate_token)
        elapsed = time.perf_counter() - start
        elapsed_ms = elapsed * 1000
        # The value was already being computed for the header and the log line;
        # recording it is what makes latency queryable. Labelled by the matched
        # route *template* so /admin/keys/{key} is one series, not one per key —
        # an unmatched request (404) has no template and is bucketed together.
        route = request.scope.get("route")
        metrics.observe_request_duration(getattr(route, "path", None) or "<unmatched>", elapsed)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.1f}"
        response.headers["X-RekAI-Version"] = __version__
        response.headers["traceparent"] = tracing.format_traceparent(trace_id, span_id)
        if tracestate:
            response.headers["tracestate"] = tracestate
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
                # OTel GenAI semantic-convention attributes for chat/embeddings
                # requests (set by _stash_gen_ai). Absent on non-LLM requests and
                # on streaming (the log line fires before the stream body is
                # consumed) — the model/provider are set pre-stream, usage isn't.
                **getattr(request.state, "gen_ai", {}),
            },
        )
        return response

    # Both of these wrap the whole app so they run before _rate_limit/auth/
    # routing see the request, and — unlike a BaseHTTPMiddleware dispatch —
    # stay wrapped around a streaming response body.
    #
    # Added first = innermost, so the ordering below is
    #   CORS → OpenAICompatError → MaxBodySize → ConcurrencyLimit → the http
    #   middlewares
    # deliberately: rejecting an oversized body is cheap and shouldn't consume
    # one of the concurrency slots it would otherwise occupy, and the envelope
    # translation has to sit outside every layer whose rejections it rewrites
    # (including the 413 and the concurrency 429 above).
    app.add_middleware(ConcurrencyLimitMiddleware, max_concurrent=settings.max_concurrent_requests)
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(OpenAICompatErrorMiddleware)

    # CORS is added last so it wraps the others (outermost): short-circuit
    # responses like a 429 from the rate limiter (or a 413 from the body-size
    # cap above) still get CORS headers, so the browser can read them instead
    # of failing the fetch.
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
            "tracestate",
            "X-Guardrail-Flag",
            "X-Redacted",
            "X-Cache-Similarity",
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
        """Liveness plus a coarse readiness signal.

        Deliberately does **no I/O**: no upstream probe, no Redis ping. An
        unauthenticated endpoint that makes the gateway call out to every
        provider on demand is a request amplifier, and a health check that can
        block on a slow dependency is worse than one that can't — the answer
        arrives from state RekAI already tracks. What it reports is real,
        though: the cooldown/circuit-breaker machinery already knew which
        providers were parked and had no way to say so, leaving `status` typed
        as a constant `"ok"` that could never be anything else.
        """
        provider_status: dict[str, Literal["ready", "byok_only"]] = {}
        for name in provider_names():
            provider = get_provider(name)
            ready = provider is not None and provider.server_key_configured()
            provider_status[name] = "ready" if ready else "byok_only"
        parked = cooldowns.parked()
        return HealthResponse(
            # Still 200 when degraded: the gateway is serving, just not from
            # every backend, and flipping an orchestrator's liveness probe over
            # one parked provider would take down a working deployment.
            status="degraded" if parked else "ok",
            version=__version__,
            providers=provider_names(),
            provider_status=provider_status,
            parked_providers=parked,
            cache=cache.label,
        )

    async def _is_authenticated(request: Request) -> bool:
        """True when the request carries a valid gateway key."""
        token = auth.parse_bearer(request.headers.get("authorization"))
        return token is not None and auth.key_allowed(token, await _allowed_keys())

    def _multi_tenant() -> bool:
        """True when gateway auth is configured, i.e. callers are distinguishable
        tenants rather than one operator poking a local instance."""
        return bool(settings.api_key_list) or key_store is not None

    @app.get("/metrics", tags=["system"], response_class=PlainTextResponse)
    async def metrics_endpoint(request: Request, response: Response) -> str:
        # /metrics is open by default so Prometheus can scrape without a token.
        # The scalar and per-provider series are operational and stay open; the
        # per-client cost/token breakdown is per-tenant data, so it is emitted
        # only to an authenticated caller once gateway auth is in use (an
        # unauthenticated scrape still gets everything else). Operators who want
        # the whole endpoint behind the key set REKAI_METRICS_REQUIRE_AUTH.
        if not _multi_tenant():
            return metrics.render()
        authenticated = await _is_authenticated(request)
        if settings.metrics_require_auth and not authenticated:
            metrics.record_error("unauthorized")
            response.status_code = 401
            response.headers["WWW-Authenticate"] = "Bearer"
            return ErrorResponse(
                error="unauthorized", detail="Missing or invalid API key."
            ).model_dump_json()
        return metrics.render(include_clients=authenticated)

    async def _fleet_snapshot() -> dict:
        """This replica's live counters plus every other replica's last-persisted
        snapshot. With no Redis (process-local) or a single replica,
        load_others() returns [] and this is just the local snapshot. /metrics
        stays per-instance for Prometheus (see metrics_store)."""
        others = await metrics_store.load_others()
        if not others:
            return metrics.snapshot()
        return merge_snapshots([metrics.snapshot(), *others], metrics.max_tracked_clients)

    @app.get("/v1/usage", response_model=UsageSummary, tags=["system"])
    async def usage_summary(http_request: Request) -> UsageSummary:
        snapshot = await _fleet_snapshot()
        if _multi_tenant():
            # usage_by_client names every tenant and what it spent. Under
            # gateway auth this endpoint is a *tenant* view, so it reports only
            # the caller's own row — an operator wanting the fleet breakdown
            # uses /admin/usage (REKAI_ADMIN_KEY). With auth off there are no
            # tenants to separate and the full map is the local operator's own.
            client_id = _client_id(http_request)
            own = snapshot.get("usage_by_client", {}).get(client_id)
            snapshot = {**snapshot, "usage_by_client": {client_id: own} if own else {}}
        return UsageSummary(**snapshot)

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

        @app.get("/admin/usage", response_model=UsageSummary, tags=["admin"])
        async def admin_usage(request: Request):
            """The fleet-wide usage view, including every tenant's breakdown.

            /v1/usage scopes usage_by_client to the calling tenant, so this is
            where an operator gets the cross-tenant picture — gated by
            REKAI_ADMIN_KEY like the rest of /admin/*, not by a tenant key."""
            rate_limited = await _admin_rate_limited(request)
            if rate_limited is not None:
                return rate_limited
            if not _admin_authorized(request):
                return _admin_auth_error()
            admin_logger.info(
                "admin read usage ip=%s",
                _admin_ip(request),
                extra={"admin_action": "read_usage", "ip": _admin_ip(request)},
            )
            return UsageSummary(**await _fleet_snapshot())

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
            409: {"model": ErrorResponse},
            413: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
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
        return await _run_chat(
            request, http_request, response, x_provider_key, idempotency_key, config, cache_backend
        )

    @app.post(
        "/v1/embeddings",
        response_model=EmbeddingsResponse,
        tags=["embeddings"],
        responses={
            400: {"model": ErrorResponse},
            401: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            413: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
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
    ) -> EmbeddingsResponse | JSONResponse:
        fingerprint: str | None = None
        claimed = False
        client_id = _client_id(http_request)
        if idempotency_key:
            fingerprint = idempotency.fingerprint(request.model_dump_json())
            outcome = await idempotency.claim(
                cache_backend,
                client_id,
                idempotency_key,
                fingerprint,
                config.idempotency_ttl_seconds,
            )
            if outcome.kind == "mismatch":
                return _idempotency_error(422, _IDEM_MISMATCH)
            if outcome.kind == "conflict":
                return _idempotency_error(409, _IDEM_CONFLICT)
            if outcome.kind == "replay" and outcome.response is not None:
                response.headers["Idempotent-Replay"] = "true"
                replayed = EmbeddingsResponse(**outcome.response)
                _record_client_usage(http_request, replayed, config, operation="embeddings")
                return replayed
            claimed = True
        try:
            result = await handle_embeddings(request, x_provider_key, config, cache_backend)
        except Exception:
            if claimed:
                await idempotency.release(cache_backend, client_id, idempotency_key)  # type: ignore[arg-type]
            raise
        _record_client_usage(http_request, result, config, operation="embeddings")
        if idempotency_key and fingerprint is not None:
            await idempotency.complete(
                cache_backend,
                client_id,
                idempotency_key,
                fingerprint,
                result.model_dump(mode="json"),
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
        then a terminating ``data: [DONE]``. Streaming responses are not cached
        and do not accept ``Idempotency-Key`` (unlike ``/v1/chat``) — a retried
        streaming request always re-runs; see docs/architecture.md.
        """
        blocked = _guardrail_response(request.messages, config, response)
        if blocked is not None:
            return blocked
        guardrail_flag = response.headers.get("X-Guardrail-Flag")
        client_id = _client_id(http_request)
        provider_name, provider = select_provider(request, config)
        metrics.record_request(provider_name)
        _stash_gen_ai_prestream(http_request, provider_name, request.model)

        async def event_source():
            async for ev in handle_chat_stream(
                request, x_provider_key, config, cache_backend, provider_name, provider, client_id
            ):
                if ev.delta is not None:
                    yield f"data: {json.dumps({'delta': ev.delta})}\n\n"
                elif ev.error is not None:
                    payload = {"error": "provider_error", "detail": str(ev.error)}
                    yield f"data: {json.dumps(payload)}\n\n"
                elif ev.summary is not None:
                    s = ev.summary
                    summary = {
                        "provider": s.provider,
                        "model": s.model,
                        "usage": s.usage.model_dump(),
                        "cost_usd": s.cost_usd,
                        "estimated": s.estimated,
                    }
                    if s.tool_calls:
                        summary["tool_calls"] = s.tool_calls
                    if s.finish_reason:
                        summary["finish_reason"] = s.finish_reason
                    if s.redacted:
                        summary["redacted"] = s.redacted
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

    @app.post(
        "/v1/chat/completions",
        tags=["chat"],
        response_model=None,
        responses={
            200: {"content": {"application/json": {}, "text/event-stream": {}}},
            400: {"model": ErrorResponse},
            401: {"model": ErrorResponse},
            403: {"model": ErrorResponse},
            413: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
        },
    )
    async def chat_completions(
        response: Response,
        request: ChatCompletionsRequest,
        http_request: Request,
        x_provider_key: str | None = Header(default=None, alias="X-Provider-Key"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        config: Settings = Depends(get_config),
        cache_backend: CacheBackend = Depends(get_cache),
    ):
        """OpenAI-compatible chat completions.

        Point any OpenAI SDK (or LangChain, etc.) at RekAI's base URL — ``.../v1``
        — and this behaves like ``POST /v1/chat/completions``: same request and
        response shapes, non-streaming and ``stream: true`` both supported. It is
        a thin translation over the same internal pipeline as ``/v1/chat`` (so
        routing, cache, retries, fallback, budgets, and metrics all apply). RekAI
        extensions: an optional ``provider`` field, or an OpenRouter-style
        ``"<provider>/<model>"`` model string, forces a provider; unknown OpenAI
        tuning params are tolerated and ignored. ``Idempotency-Key`` is honored
        on the non-streaming path only — ``stream: true`` does not accept it,
        same as ``/v1/chat/stream``; see docs/architecture.md.
        """
        # This route does not wrap its own errors in the OpenAI envelope:
        # OpenAICompatErrorMiddleware translates every error on this path,
        # including the ones raised below and the ones the middlewares above
        # produce before this function is ever called. Letting ProviderError
        # propagate is what keeps `_provider_error_handler`'s metrics and its
        # `Retry-After` on an upstream 429.
        try:
            chat_request = openai_compat.to_chat_request(request)
        except ValidationError as exc:
            # Not a ProviderError, so no handler upstream turns it into a
            # response; without this it would surface as a 500.
            return JSONResponse(
                status_code=400,
                content=ErrorResponse(error="invalid_request", detail=str(exc)).model_dump(),
            )

        if not request.stream:
            result = await _run_chat(
                chat_request,
                http_request,
                response,
                x_provider_key,
                idempotency_key,
                config,
                cache_backend,
            )
            # A guardrail block or an Idempotency-Key conflict — already a RekAI
            # error body, so pass it along rather than guessing at its meaning.
            if isinstance(result, JSONResponse):
                return result
            return openai_compat.to_chat_completion(result)

        # Streaming.
        blocked = _guardrail_response(chat_request.messages, config, response)
        if blocked is not None:
            return blocked
        guardrail_flag = response.headers.get("X-Guardrail-Flag")
        client_id = _client_id(http_request)
        provider_name, provider = select_provider(chat_request, config)
        metrics.record_request(provider_name)
        _stash_gen_ai_prestream(http_request, provider_name, chat_request.model)

        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        model = chat_request.model
        include_usage = request.stream_options is not None and request.stream_options.include_usage

        async def event_source():
            def sse(obj: dict) -> str:
                return f"data: {json.dumps(obj)}\n\n"

            yield sse(openai_compat.chunk_first(chunk_id, created, model))
            finish_reason = "stop"
            async for ev in handle_chat_stream(
                chat_request,
                x_provider_key,
                config,
                cache_backend,
                provider_name,
                provider,
                client_id,
            ):
                if ev.delta is not None:
                    yield sse(openai_compat.chunk_delta(chunk_id, created, model, ev.delta))
                elif ev.error is not None:
                    yield sse(openai_compat.openai_error(ev.error.status_code, str(ev.error)))
                    yield "data: [DONE]\n\n"
                    return
                elif ev.summary is not None:
                    # Prefer what the provider actually said; fall back to the
                    # derived value only when it said nothing.
                    if ev.summary.finish_reason:
                        finish_reason = ev.summary.finish_reason
                    if ev.summary.tool_calls:
                        finish_reason = "tool_calls"
                        yield sse(
                            openai_compat.chunk_tool_calls(
                                chunk_id, created, model, ev.summary.tool_calls
                            )
                        )
                    yield sse(openai_compat.chunk_finish(chunk_id, created, model, finish_reason))
                    if include_usage:
                        yield sse(
                            openai_compat.chunk_usage(chunk_id, created, model, ev.summary.usage)
                        )
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
