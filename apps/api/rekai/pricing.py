"""Approximate model pricing and cost estimation.

Prices are USD per 1,000,000 tokens as ``(input, output)`` and are necessarily
approximate — providers change them and they vary by region/tier. They exist so
RekAI can surface a *rough* cost per request for budgeting, not billing.

Override or extend the table via :func:`register_price` (e.g. from a plugin or
config) without touching this module.
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
}


def register_price(model_prefix: str, input_per_1m: float, output_per_1m: float) -> None:
    """Add or override pricing for models matching ``model_prefix``."""
    _PRICES_PER_1M[model_prefix] = (input_per_1m, output_per_1m)


def price_for_model(model: str) -> tuple[float, float] | None:
    """Return ``(input, output)`` per-1M price for a model, or None if unknown.

    Matches the longest known prefix so ``gpt-4o-mini-2024`` resolves to the
    ``gpt-4o-mini`` price rather than ``gpt-4o``.
    """
    model = model.lower()
    best: tuple[float, float] | None = None
    best_len = -1
    for prefix, price in _PRICES_PER_1M.items():
        if model.startswith(prefix) and len(prefix) > best_len:
            best, best_len = price, len(prefix)
    return best


def estimate_cost(provider: str, model: str, usage: Usage) -> float | None:
    """Estimate the USD cost of a response.

    Returns ``0.0`` for free/local providers, a positive estimate when the model
    is priced, and ``None`` when the price is unknown.
    """
    if provider in FREE_PROVIDERS:
        return 0.0
    price = price_for_model(model)
    if price is None:
        return None
    input_per_1m, output_per_1m = price
    cost = (
        usage.prompt_tokens * input_per_1m + usage.completion_tokens * output_per_1m
    ) / 1_000_000
    return round(cost, 6)
