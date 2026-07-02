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

from rekai.schemas import Usage

# Providers whose usage is free to the operator (local or echo).
FREE_PROVIDERS: set[str] = {"echo", "ollama"}

# model-id prefix -> (input_per_1m, output_per_1m) in USD.
_PRICES_PER_1M: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4": (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "o1-mini": (1.10, 4.40),
    "o1": (15.00, 60.00),
    "o3-mini": (1.10, 4.40),
    # Anthropic
    "claude-opus": (15.00, 75.00),
    "claude-sonnet": (3.00, 15.00),
    "claude-haiku": (0.80, 4.00),
    # Google Gemini
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
    # Embeddings (input-only; output price is 0).
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
    "text-embedding-ada-002": (0.10, 0.0),
}


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
    cost = (
        usage.prompt_tokens * input_per_1m + usage.completion_tokens * output_per_1m
    ) / 1_000_000
    return round(cost, 6)
