"""Approximate model pricing and cost estimation.

Prices are USD per 1,000,000 tokens as ``(input, output)`` and are necessarily
approximate — providers change them and they vary by region/tier. They exist so
RekAI can surface a *rough* cost per request for budgeting, not billing.

Override or extend the table two ways:

- :func:`register_price` mutates the built-in table directly — for a plugin or
  code that runs once at import time (process-wide, affects every ``Settings``
  instance).
- Pass an ``overrides`` dict (see ``Settings.pricing_override_dict``, built
  from ``REKAI_PRICING_OVERRIDES``) to :func:`price_for_model`/:func:`estimate_cost`
  — a config-driven override, scoped to one deployment's settings rather than
  mutating shared global state (so tests using different ``Settings`` don't
  bleed into each other).
"""

from __future__ import annotations

from rekai.models import price_table
from rekai.schemas import Usage

# Providers whose usage is free to the operator (local or echo).
FREE_PROVIDERS: set[str] = {"echo", "ollama"}

# Prompt-cache multipliers applied to the *input* price. Reading a cached prefix
# is ~10x cheaper than sending it fresh; writing one costs a premium. These match
# Anthropic's published ratios and are close enough for OpenAI's automatic
# caching, which only ever reports reads.
_CACHE_READ_MULTIPLIER = 0.1
_CACHE_WRITE_MULTIPLIER = 1.25

# model-id prefix -> (input_per_1m, output_per_1m) in USD, derived from the
# single model registry (rekai/models.py) so pricing can't drift from routing
# and the advertised model list. register_price() still mutates this in place.
_PRICES_PER_1M: dict[str, tuple[float, float]] = price_table()


def register_price(model_prefix: str, input_per_1m: float, output_per_1m: float) -> None:
    """Add or override pricing for models matching ``model_prefix``."""
    _PRICES_PER_1M[model_prefix] = (input_per_1m, output_per_1m)


def price_for_model(
    model: str, overrides: dict[str, tuple[float, float]] | None = None
) -> tuple[float, float] | None:
    """Return ``(input, output)`` per-1M price for a model, or None if unknown.

    Matches the longest known prefix so ``gpt-4o-mini-2024`` resolves to the
    ``gpt-4o-mini`` price rather than ``gpt-4o``. ``overrides`` entries win over
    the built-in table for the same prefix (a plain dict union keeps the
    override's value on a key collision) — new prefixes just add pricing for
    an otherwise-unknown model.
    """
    model = model.lower()
    table = {**_PRICES_PER_1M, **overrides} if overrides else _PRICES_PER_1M
    best: tuple[float, float] | None = None
    best_len = -1
    for prefix, price in table.items():
        if model.startswith(prefix) and len(prefix) > best_len:
            best, best_len = price, len(prefix)
    return best


def estimate_tokens(text: str) -> int:
    """A naive token estimate (whitespace words). Used where the provider does
    not report exact usage — e.g. the text-only streaming path."""
    return max(1, len(text.split()))


def estimate_cost(
    provider: str,
    model: str,
    usage: Usage,
    pricing_overrides: dict[str, tuple[float, float]] | None = None,
) -> float | None:
    """Estimate the USD cost of a response.

    Returns ``0.0`` for free/local providers, a positive estimate when the model
    is priced, and ``None`` when the price is unknown.
    """
    if provider in FREE_PROVIDERS:
        return 0.0
    price = price_for_model(model, pricing_overrides)
    if price is None:
        return None
    input_per_1m, output_per_1m = price
    # cache_read/cache_write are a breakdown of prompt_tokens, so bill the
    # remainder at full input price and each cached slice at its own rate.
    cached = usage.cache_read_tokens + usage.cache_write_tokens
    uncached_prompt = max(0, usage.prompt_tokens - cached)
    cost = (
        uncached_prompt * input_per_1m
        + usage.cache_read_tokens * input_per_1m * _CACHE_READ_MULTIPLIER
        + usage.cache_write_tokens * input_per_1m * _CACHE_WRITE_MULTIPLIER
        + usage.completion_tokens * output_per_1m
    ) / 1_000_000
    return round(cost, 6)
