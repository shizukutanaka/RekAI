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


# Character ranges that a subword tokenizer generally splits at ~1 token each:
# kana, CJK ideographs (+ ext A and the SIP), Hangul, and the fullwidth/CJK
# symbol forms. Latin/Cyrillic/etc. text is far denser per token, so it is
# estimated separately at the ~4-chars-per-token rule below.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3000, 0x30FF),  # CJK symbols & punctuation, hiragana, katakana
    (0x3400, 0x4DBF),  # CJK unified ideographs extension A
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0xAC00, 0xD7AF),  # Hangul syllables
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0xFF00, 0xFFEF),  # halfwidth & fullwidth forms
    (0x20000, 0x2A6DF),  # CJK unified ideographs extension B
)

# OpenAI's published rule of thumb for Latin-script text.
_CHARS_PER_TOKEN = 4.0


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """Estimate a token count from character classes, script-aware.

    Only used where the provider does not report exact usage — the text-only
    streaming fallback — but that estimate still feeds ``cost_usd`` and the
    per-client **budget cap**, so being wrong here silently under-bills real
    tenants.

    The previous heuristic was ``len(text.split())`` — a whitespace word count.
    That undercounts Latin text by ~30% (subword tokenizers split most words
    into >1 token) and is catastrophic for scripts without spaces: a 200-
    character Japanese or Chinese reply has essentially one "word", so it was
    counted as ~1 token — a 100x+ undercount, which let a CJK-language app blow
    past its budget effectively unmetered.

    Instead: CJK/kana/Hangul characters are counted ~1 token each (a subword
    tokenizer rarely merges them), and the rest at OpenAI's ~4-chars-per-token
    rule. Measured against ``o200k_base`` this lands within ~15% across English,
    Japanese, Chinese, and Korean, and errs slightly high — the safe direction
    for a spend cap. It stays a heuristic, not a tokenizer: a real one
    (tiktoken) needs a per-model vocab download that a self-hosted, possibly
    air-gapped, deployment can't assume, and providers that report exact usage
    never reach this path.
    """
    if not text:
        return 1
    cjk = sum(1 for ch in text if _is_cjk(ch))
    other = len(text) - cjk
    return max(1, round(cjk + other / _CHARS_PER_TOKEN))


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
