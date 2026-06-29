import pytest

from rekai.pricing import estimate_cost, price_for_model, register_price
from rekai.schemas import Usage


def test_free_providers_cost_zero() -> None:
    usage = Usage(prompt_tokens=100, completion_tokens=100, total_tokens=200)
    assert estimate_cost("echo", "echo", usage) == 0.0
    assert estimate_cost("ollama", "llama3.1", usage) == 0.0


def test_unknown_model_returns_none() -> None:
    usage = Usage(prompt_tokens=10, completion_tokens=10, total_tokens=20)
    assert estimate_cost("openai", "mystery-model-x", usage) is None


def test_known_model_cost() -> None:
    # gpt-4o-mini: 0.15 in / 0.60 out per 1M tokens.
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000)
    cost = estimate_cost("openai", "gpt-4o-mini", usage)
    assert cost == pytest.approx(0.75)


def test_longest_prefix_wins() -> None:
    # gpt-4o-mini-2024 should match gpt-4o-mini, not gpt-4o.
    assert price_for_model("gpt-4o-mini-2024-07-18") == (0.15, 0.60)
    assert price_for_model("gpt-4o-2024") == (2.50, 10.00)


def test_register_price_override() -> None:
    register_price("test-model", 1.0, 2.0)
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=0, total_tokens=1_000_000)
    assert estimate_cost("openai", "test-model", usage) == pytest.approx(1.0)
