"""Chat orchestration: route → cache lookup → provider call (+ fallback) → cache."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from rekai.cache import CacheBackend, cache_key, embedding_cache_key
from rekai.circuit_breaker import consecutive_failures
from rekai.config import Settings
from rekai.cooldown import cooldowns
from rekai.logging_config import get_logger
from rekai.metrics import metrics
from rekai.pricing import estimate_cost
from rekai.providers import Provider, get_provider
from rekai.providers.base import ProviderError, ProviderResult
from rekai.retry import call_with_retry
from rekai.router import resolve_provider, select_provider
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

    # Request-level fallbacks take precedence over the server default chain.
    if request.fallbacks is not None:
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


async def _semantic_embed(request: ChatRequest, settings: Settings) -> list[float] | None:
    """Embed the prompt for the semantic cache (server-side key), or None."""
    provider = get_provider(resolve_provider(None, settings.semantic_cache_model, settings))
    if provider is None:
        return None
    text = "\n".join(m.content or "" for m in request.messages)
    try:
        result = await provider.embed([text], settings.semantic_cache_model, None)
    except ProviderError:
        return None  # embeddings unavailable -> just skip the semantic cache
    return result.embeddings[0] if result.embeddings else None


async def handle_chat(
    request: ChatRequest,
    api_key: str | None,
    settings: Settings,
    cache: CacheBackend,
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
        sem_bucket = f"{primary_name}:{request.model}:{request.temperature}:{request.max_tokens}"
        sem_embedding = await _semantic_embed(request, settings)
        if sem_embedding is not None:
            hit = semantic_cache.find(sem_bucket, sem_embedding, settings.semantic_cache_threshold)
            if hit is not None:
                metrics.record_cache(hit=True)
                logger.info("semantic cache hit model=%s", request.model)
                return ChatResponse(**{**json.loads(hit), "cached": True})

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

        try:
            result = await call_with_retry(
                _chat_factory(attempt.provider, attempt_request, api_key),
                attempts=settings.retry_max_attempts,
                base_delay=settings.retry_base_delay_seconds,
                max_delay=settings.retry_max_delay_seconds,
                on_retry=metrics.record_retry,
            )
        except ProviderError as exc:
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
                metrics.record_error()
                logger.warning(
                    "provider %s failed (%s); trying fallback",
                    attempt.provider_name,
                    exc.status_code,
                )
                continue
            raise

        consecutive_failures.record_success(attempt.provider_name)
        usage = result.usage or Usage()
        cost_usd = estimate_cost(attempt.provider_name, result.model, usage)
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
            created=int(time.time()),
        )

        if use_cache:
            await cache.set(key, response.model_dump_json(), settings.cache_ttl_seconds)
        if sem_enabled and sem_embedding is not None:
            semantic_cache.add(sem_bucket, sem_embedding, response.model_dump_json())

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


async def handle_embeddings(
    request: EmbeddingsRequest,
    api_key: str | None,
    settings: Settings,
    cache: CacheBackend,
) -> EmbeddingsResponse:
    provider_name = resolve_provider(request.provider, request.model, settings)
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

    result = await call_with_retry(
        lambda: provider.embed(inputs, request.model, api_key),
        attempts=settings.retry_max_attempts,
        base_delay=settings.retry_base_delay_seconds,
        max_delay=settings.retry_max_delay_seconds,
        on_retry=metrics.record_retry,
    )
    metrics.record_tokens(result.usage.total_tokens)
    cost_usd = estimate_cost(provider_name, result.model, result.usage)
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
