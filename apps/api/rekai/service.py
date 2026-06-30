"""Chat orchestration: route → cache lookup → provider call (+ fallback) → cache."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from rekai.cache import CacheBackend, cache_key, embedding_cache_key
from rekai.config import Settings
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


async def handle_chat(
    request: ChatRequest,
    api_key: str | None,
    settings: Settings,
    cache: CacheBackend,
) -> ChatResponse:
    primary_name, primary = select_provider(request, settings)
    attempts = _build_attempts(request, primary_name, primary, settings)
    use_cache = settings.cache_enabled and request.cache

    last_error: ProviderError | None = None

    for index, attempt in enumerate(attempts):
        is_fallback = index > 0
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
            )
        except ProviderError as exc:
            last_error = exc
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
