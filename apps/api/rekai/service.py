"""Chat orchestration: route → cache lookup → provider call → cache store."""

from __future__ import annotations

import json
import time
import uuid

from rekai.cache import CacheBackend, cache_key
from rekai.config import Settings
from rekai.logging_config import get_logger
from rekai.metrics import metrics
from rekai.pricing import estimate_cost
from rekai.router import select_provider
from rekai.schemas import ChatRequest, ChatResponse, Usage

logger = get_logger("rekai.service")


async def handle_chat(
    request: ChatRequest,
    api_key: str | None,
    settings: Settings,
    cache: CacheBackend,
) -> ChatResponse:
    provider_name, provider = select_provider(request, settings)
    metrics.record_request(provider_name)

    key = cache_key(request, provider_name)
    use_cache = settings.cache_enabled and request.cache

    if use_cache:
        cached_raw = await cache.get(key)
        if cached_raw is not None:
            metrics.record_cache(hit=True)
            logger.info("cache hit provider=%s model=%s", provider_name, request.model)
            payload = json.loads(cached_raw)
            return ChatResponse(**{**payload, "cached": True})
        metrics.record_cache(hit=False)

    result = await provider.chat(request, api_key)
    usage = result.usage or Usage()
    cost_usd = estimate_cost(provider_name, result.model, usage)
    metrics.record_tokens(usage.total_tokens)
    metrics.record_cost(cost_usd)

    response = ChatResponse(
        id=f"rekai-{uuid.uuid4().hex[:24]}",
        provider=provider_name,
        model=result.model,
        content=result.content,
        usage=usage,
        cost_usd=cost_usd,
        cached=False,
        created=int(time.time()),
    )

    if use_cache:
        await cache.set(key, response.model_dump_json(), settings.cache_ttl_seconds)

    logger.info(
        "chat ok provider=%s model=%s tokens=%s",
        provider_name,
        response.model,
        response.usage.total_tokens,
    )
    return response
