"""Chat orchestration: route → cache lookup → provider call (+ fallback) → cache."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from rekai import guardrails
from rekai.cache import CacheBackend, cache_key, embedding_cache_key, semantic_bucket
from rekai.circuit_breaker import consecutive_failures
from rekai.config import Settings
from rekai.cooldown import cooldowns
from rekai.logging_config import get_logger
from rekai.metrics import metrics
from rekai.pricing import estimate_cost, estimate_tokens
from rekai.providers import Provider, get_provider
from rekai.providers.base import ProviderError, ProviderResult
from rekai.retry import call_with_retry
from rekai.router import ensure_allowed, resolve_provider, select_provider
from rekai.schemas import (
    ChatRequest,
    ChatResponse,
    EmbeddingsRequest,
    EmbeddingsResponse,
    Usage,
)
from rekai.semantic_cache import semantic_cache

logger = get_logger("rekai.service")


@dataclass
class _Attempt:
    provider_name: str
    provider: Provider
    model: str


@dataclass
class StreamSummary:
    """The terminal accounting event of a streamed completion."""

    provider: str
    model: str
    usage: Usage
    cost_usd: float | None
    estimated: bool
    tool_calls: list[dict] | None = None
    finish_reason: str | None = None
    # Secret patterns scrubbed from the streamed text. Reported here rather
    # than as a header because response headers are long gone by the time the
    # first delta is redacted.
    redacted: list[str] | None = None


@dataclass
class ChatStreamEvent:
    """One event from :func:`handle_chat_stream`.

    Exactly one field is set: a text ``delta``, a terminal ``error``, or the
    terminal ``summary``. The transport layer decides how to serialize each
    (native SSE vs OpenAI-compatible chunks), so the pipeline stays format-
    agnostic and is shared by both streaming endpoints.
    """

    delta: str | None = None
    error: ProviderError | None = None
    summary: StreamSummary | None = None


def _chat_factory(
    provider: Provider, request: ChatRequest, api_key: str | None
) -> Callable[[], Awaitable[ProviderResult]]:
    """A zero-arg coroutine factory bound to one attempt (so retry can re-call it)."""

    async def _do() -> ProviderResult:
        return await provider.chat(request, api_key)

    return _do


def _build_attempts(
    request: ChatRequest, primary_name: str, primary: Provider, settings: Settings
) -> list[_Attempt]:
    """Primary attempt followed by the resolved fallback chain."""
    attempts = [_Attempt(primary_name, primary, request.model)]

    # Request-level fallbacks take precedence over the server default chain —
    # but only among providers the operator has enabled for requests, since
    # this is a client-steered choice like `provider` itself. A disallowed
    # target is a 403, not a silent skip: quietly dropping it would leave the
    # client believing it had a fallback chain it doesn't have.
    if request.fallbacks is not None:
        for target in request.fallbacks:
            ensure_allowed(target.provider, settings)
        targets: list[tuple[str, str | None]] = [(f.provider, f.model) for f in request.fallbacks]
    elif settings.fallback_enabled:
        targets = settings.fallback_target_list
    else:
        targets = []

    for name, model in targets:
        provider = get_provider(name)
        if provider is None:
            logger.warning("skipping unknown fallback provider '%s'", name)
            continue
        if name == primary_name and (model or request.model) == request.model:
            continue  # don't retry the identical primary attempt
        attempts.append(_Attempt(name, provider, model or request.model))
    return attempts


def _prompt_text(request: ChatRequest) -> str:
    """The text the semantic cache embeds and guards on — one definition, so the
    vector and the discriminator digest can't be derived from different strings."""
    return "\n".join(m.content or "" for m in request.messages)


async def _semantic_embed(request: ChatRequest, settings: Settings) -> list[float] | None:
    """Embed the prompt for the semantic cache (server-side key), or None.

    This is a real upstream call the caller never asked for, billed to the
    operator's key, so its tokens and cost are recorded like any other — an
    unmetered call would make the semantic cache look strictly cheaper than it
    is, which is the opposite of what a cost-aware gateway should report."""
    provider_name = resolve_provider(None, settings.semantic_cache_model, settings)
    provider = get_provider(provider_name)
    if provider is None:
        return None
    text = _prompt_text(request)
    started = time.perf_counter()
    try:
        result = await provider.embed([text], settings.semantic_cache_model, None)
    except ProviderError:
        return None  # embeddings unavailable -> just skip the semantic cache
    metrics.observe_provider_duration(provider_name, "embed", time.perf_counter() - started)
    metrics.record_tokens(result.usage.total_tokens)
    metrics.record_cost(
        estimate_cost(provider_name, result.model, result.usage, settings.pricing_override_dict)
    )
    return result.embeddings[0] if result.embeddings else None


def _redact(response: ChatResponse, settings: Settings) -> ChatResponse:
    """Scrub secret/API-key patterns out of the assistant's content (OWASP LLM02).

    Deliberately called *before* the response is written to the response cache,
    the semantic cache, or the idempotency store: a secret that reaches any of
    those persists there — in Redis, in plaintext, for the whole TTL — and every
    later replay would have to re-scrub it. Redacting at the source means the
    raw secret is never stored in the first place, which is what
    ``docs/architecture.md`` promises. The pattern names ride along on the
    response so a cache hit can still report ``X-Redacted``.
    """
    if not settings.output_redaction_enabled or not response.content:
        return response
    scrubbed, hits = guardrails.redact_secrets(response.content)
    if not hits:
        return response
    return response.model_copy(update={"content": scrubbed, "redacted": hits})


async def handle_chat(
    request: ChatRequest,
    api_key: str | None,
    settings: Settings,
    cache: CacheBackend,
    client_id: str = "anonymous",
) -> ChatResponse:
    primary_name, primary = select_provider(request, settings)
    attempts = _build_attempts(request, primary_name, primary, settings)
    use_cache = settings.cache_enabled and request.cache

    # Semantic cache (its own in-memory store): reuse a response for a paraphrase
    # of an earlier prompt. Respects the per-request opt-out, independent of the
    # content-cache backend.
    sem_enabled = settings.semantic_cache_enabled and request.cache
    sem_bucket = ""
    sem_embedding: list[float] | None = None
    if sem_enabled:
        sem_bucket = semantic_bucket(request, primary_name, client_id)
        sem_prompt = _prompt_text(request)
        sem_embedding = await _semantic_embed(request, settings)
        if sem_embedding is not None:
            lookup_started = time.perf_counter()
            hit = semantic_cache.find(
                sem_bucket, sem_prompt, sem_embedding, settings.semantic_cache_threshold
            )
            metrics.observe_semantic_lookup(
                "hit" if hit is not None else "miss", time.perf_counter() - lookup_started
            )
            if hit is not None:
                payload, similarity = hit
                metrics.record_cache(hit=True)
                metrics.record_semantic_cache_hit()
                logger.info(
                    "semantic cache hit model=%s similarity=%.4f", request.model, similarity
                )
                # cache_similarity is what distinguishes this from an exact hit:
                # the answer is to a *different* prompt, and how different is
                # the caller's business. Overwrites whatever was stored (always
                # None — only fresh responses are stored) with this hit's score.
                return ChatResponse(
                    **{
                        **json.loads(payload),
                        "cached": True,
                        "cache_similarity": round(similarity, 4),
                    }
                )

    last_error: ProviderError | None = None

    for index, attempt in enumerate(attempts):
        is_fallback = index > 0
        # Skip a provider that's cooling down from a recent 429 — unless it's the
        # only target left (better to try than to fail). Consults the shared
        # (Redis) backend too, so a cooldown set by another worker/node is honored.
        if (
            settings.provider_cooldown_enabled
            and index + 1 < len(attempts)
            and await cooldowns.active_shared(cache, attempt.provider_name)
        ):
            logger.info(
                "skipping %s (cooling down %.0fs)",
                attempt.provider_name,
                cooldowns.remaining(attempt.provider_name),
            )
            continue
        attempt_request = (
            request
            if attempt.model == request.model
            else request.model_copy(update={"model": attempt.model})
        )
        metrics.record_request(attempt.provider_name)
        if is_fallback:
            metrics.record_fallback()

        key = cache_key(attempt_request, attempt.provider_name)
        if use_cache:
            cached_raw = await cache.get(key)
            if cached_raw is not None:
                metrics.record_cache(hit=True)
                logger.info("cache hit provider=%s model=%s", attempt.provider_name, attempt.model)
                payload = json.loads(cached_raw)
                return ChatResponse(**{**payload, "cached": True})
            metrics.record_cache(hit=False)

        started = time.perf_counter()
        try:
            result = await call_with_retry(
                _chat_factory(attempt.provider, attempt_request, api_key),
                attempts=settings.retry_max_attempts,
                base_delay=settings.retry_base_delay_seconds,
                max_delay=settings.retry_max_delay_seconds,
                on_retry=metrics.record_retry,
            )
        except ProviderError as exc:
            metrics.observe_provider_duration(
                attempt.provider_name, "chat", time.perf_counter() - started
            )
            # Recorded for every upstream failure, not only the ones that
            # trigger a fallback below — a per-provider success rate needs the
            # full denominator, and the fallback branch alone drops the last
            # attempt in a chain and every non-transient 4xx.
            metrics.record_provider_error(attempt.provider_name, exc.status_code)
            last_error = exc
            if settings.provider_cooldown_enabled and exc.status_code == 429:
                # An explicit "back off" signal — park this provider immediately
                # (locally and in the shared backend, when configured) so later
                # requests, including on other workers/nodes, route around it.
                await cooldowns.mark_shared(
                    cache,
                    attempt.provider_name,
                    exc.retry_after
                    if exc.retry_after is not None
                    else settings.provider_cooldown_seconds,
                )
                metrics.record_cooldown()
            elif settings.provider_cooldown_enabled and exc.status_code >= 500:
                # No explicit signal here, so require a few consecutive failures
                # (across separate requests) before parking — a lightweight
                # circuit breaker, not an overreaction to one bad request.
                failures = consecutive_failures.record_failure(attempt.provider_name)
                if failures >= settings.circuit_breaker_threshold:
                    await cooldowns.mark_shared(
                        cache, attempt.provider_name, settings.provider_cooldown_seconds
                    )
                    metrics.record_cooldown()
            # Fall through on upstream failures and rate limits (after in-place
            # retries), but not on other client (4xx) errors.
            transient = exc.status_code >= 500 or exc.status_code == 429
            if transient and index + 1 < len(attempts):
                metrics.record_error("provider_error")
                logger.warning(
                    "provider %s failed (%s); trying fallback",
                    attempt.provider_name,
                    exc.status_code,
                )
                continue
            raise

        metrics.observe_provider_duration(
            attempt.provider_name, "chat", time.perf_counter() - started
        )
        consecutive_failures.record_success(attempt.provider_name)
        usage = result.usage or Usage()
        cost_usd = estimate_cost(
            attempt.provider_name, result.model, usage, settings.pricing_override_dict
        )
        metrics.record_tokens(usage.total_tokens)
        metrics.record_cost(cost_usd)

        response = ChatResponse(
            id=f"rekai-{uuid.uuid4().hex[:24]}",
            provider=attempt.provider_name,
            model=result.model,
            content=result.content,
            tool_calls=result.tool_calls,
            usage=usage,
            cost_usd=cost_usd,
            cached=False,
            fallback_used=is_fallback,
            finish_reason=result.finish_reason,
            created=int(time.time()),
        )
        # Redact before *any* store below sees the content (see _redact).
        response = _redact(response, settings)

        if use_cache:
            await cache.set(key, response.model_dump_json(), settings.cache_ttl_seconds)
        if sem_enabled and sem_embedding is not None:
            semantic_cache.add(
                sem_bucket,
                sem_prompt,
                sem_embedding,
                response.model_dump_json(),
                settings.cache_ttl_seconds,
            )

        logger.info(
            "chat ok provider=%s model=%s tokens=%s fallback=%s",
            attempt.provider_name,
            response.model,
            response.usage.total_tokens,
            is_fallback,
        )
        return response

    # Exhausted all attempts.
    assert last_error is not None
    raise last_error


async def handle_chat_stream(
    request: ChatRequest,
    api_key: str | None,
    settings: Settings,
    cache: CacheBackend,
    provider_name: str,
    provider: Provider,
    client_id: str,
) -> AsyncIterator[ChatStreamEvent]:
    """Drive a streaming completion through one provider, yielding typed events.

    Emits text ``delta`` events as they arrive, then exactly one terminal event:
    an ``error`` (with the provider parked for cooldown, consistent with the
    non-streaming path) or a ``summary`` carrying usage/cost (provider-reported
    when available, else estimated from the streamed text). Metrics, cooldown,
    circuit-breaker, and per-client accounting side effects happen here so both
    the native and OpenAI-compatible streaming routes share them. There is no
    fallback chain on the streaming path (single provider, no in-request
    rerouting); the cooldown only benefits subsequent requests.

    ``select_provider`` and the initial ``metrics.record_request`` stay in the
    route because the resolved provider name is needed for the response header
    before streaming begins.
    """
    completion: list[str] = []
    reported_usage: Usage | None = None
    reported_tool_calls: list[dict] | None = None
    reported_finish_reason: str | None = None
    errored = False
    started = time.perf_counter()
    first_token_at: float | None = None
    # Output redaction applies here too, incrementally (see StreamRedactor).
    # It holds back a few characters of every delta, so it is only constructed
    # when actually enabled.
    redactor = guardrails.StreamRedactor() if settings.output_redaction_enabled else None
    try:
        async for event in provider.stream_events(request, api_key):
            if event.delta:
                if first_token_at is None:
                    # Time to first token: on a stream this is what the user
                    # perceives as latency. Total duration is dominated by how
                    # long the answer is and says nothing about responsiveness.
                    first_token_at = time.perf_counter()
                    metrics.observe_stream_ttft(provider_name, first_token_at - started)
                # Token accounting follows what the provider generated, not what
                # survived redaction — that is what was billed upstream.
                completion.append(event.delta)
                emitted = redactor.feed(event.delta) if redactor is not None else event.delta
                if emitted:
                    yield ChatStreamEvent(delta=emitted)
            if event.usage is not None:
                reported_usage = event.usage
            if event.tool_calls is not None:
                reported_tool_calls = event.tool_calls
            if event.finish_reason is not None:
                reported_finish_reason = event.finish_reason
        if redactor is not None:
            tail = redactor.flush()
            if tail:
                yield ChatStreamEvent(delta=tail)
    except ProviderError as exc:
        errored = True
        metrics.record_error("provider_error")
        metrics.record_provider_error(provider_name, exc.status_code)
        metrics.observe_provider_duration(provider_name, "stream", time.perf_counter() - started)
        if settings.provider_cooldown_enabled and exc.status_code == 429:
            await cooldowns.mark_shared(
                cache,
                provider_name,
                exc.retry_after
                if exc.retry_after is not None
                else settings.provider_cooldown_seconds,
            )
            metrics.record_cooldown()
        elif settings.provider_cooldown_enabled and exc.status_code >= 500:
            failures = consecutive_failures.record_failure(provider_name)
            if failures >= settings.circuit_breaker_threshold:
                await cooldowns.mark_shared(
                    cache, provider_name, settings.provider_cooldown_seconds
                )
                metrics.record_cooldown()
        yield ChatStreamEvent(error=exc)

    if not errored:
        metrics.observe_provider_duration(provider_name, "stream", time.perf_counter() - started)
        consecutive_failures.record_success(provider_name)
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
            provider_name, request.model, usage, settings.pricing_override_dict
        )
        metrics.record_tokens(usage.total_tokens)
        metrics.record_cost(cost_usd)
        metrics.record_client_usage(client_id, usage.total_tokens, cost_usd)
        if settings.client_budget_window_seconds is not None:
            metrics.record_client_budget_usage(
                client_id, cost_usd, settings.client_budget_window_seconds, time.time()
            )
        yield ChatStreamEvent(
            summary=StreamSummary(
                provider=provider_name,
                model=request.model,
                usage=usage,
                cost_usd=cost_usd,
                estimated=estimated,
                tool_calls=reported_tool_calls or None,
                finish_reason=reported_finish_reason,
                redacted=(redactor.hits or None) if redactor is not None else None,
            )
        )


async def handle_embeddings(
    request: EmbeddingsRequest,
    api_key: str | None,
    settings: Settings,
    cache: CacheBackend,
) -> EmbeddingsResponse:
    provider_name = resolve_provider(request.provider, request.model, settings)
    ensure_allowed(provider_name, settings)
    provider = get_provider(provider_name)
    if provider is None:
        raise ProviderError(f"Unknown provider '{provider_name}'.", status_code=400)
    metrics.record_request(provider_name)

    inputs = [request.input] if isinstance(request.input, str) else list(request.input)
    if not inputs:
        raise ProviderError("'input' must not be empty.", status_code=422)

    use_cache = settings.cache_enabled and request.cache
    key = embedding_cache_key(provider_name, request.model, inputs)
    if use_cache:
        cached_raw = await cache.get(key)
        if cached_raw is not None:
            metrics.record_cache(hit=True)
            return EmbeddingsResponse(**{**json.loads(cached_raw), "cached": True})
        metrics.record_cache(hit=False)

    started = time.perf_counter()
    try:
        result = await call_with_retry(
            lambda: provider.embed(inputs, request.model, api_key),
            attempts=settings.retry_max_attempts,
            base_delay=settings.retry_base_delay_seconds,
            max_delay=settings.retry_max_delay_seconds,
            on_retry=metrics.record_retry,
        )
    finally:
        metrics.observe_provider_duration(provider_name, "embed", time.perf_counter() - started)
    metrics.record_tokens(result.usage.total_tokens)
    cost_usd = estimate_cost(
        provider_name, result.model, result.usage, settings.pricing_override_dict
    )
    metrics.record_cost(cost_usd)
    response = EmbeddingsResponse(
        provider=provider_name,
        model=result.model,
        embeddings=result.embeddings,
        usage=result.usage,
        cost_usd=cost_usd,
        cached=False,
    )
    if use_cache:
        await cache.set(key, response.model_dump_json(), settings.cache_ttl_seconds)
    logger.info("embeddings ok provider=%s model=%s n=%s", provider_name, result.model, len(inputs))
    return response
