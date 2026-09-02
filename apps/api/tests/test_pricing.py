import pytest

from rekai.config import Settings
from rekai.pricing import estimate_cost, estimate_tokens, price_for_model, register_price
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


def test_overrides_param_replaces_a_known_prefix() -> None:
    # A config-driven override should win over the built-in gpt-4o price
    # without mutating the global table (register_price does that instead).
    overrides = {"gpt-4o": (1.0, 2.0)}
    assert price_for_model("gpt-4o-2024", overrides) == (1.0, 2.0)
    # The global table itself is untouched.
    assert price_for_model("gpt-4o-2024") == (2.50, 10.00)


def test_overrides_param_adds_an_unknown_model() -> None:
    overrides = {"my-custom-model": (0.5, 1.5)}
    assert price_for_model("my-custom-model-v2") is None  # unknown without the override
    assert price_for_model("my-custom-model-v2", overrides) == (0.5, 1.5)
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000)
    cost = estimate_cost("custom", "my-custom-model-v2", usage, overrides)
    assert cost == pytest.approx(2.0)


def test_overrides_longest_prefix_still_wins_across_both_tables() -> None:
    # A longer override prefix beats a shorter built-in one, and vice versa.
    overrides = {"gpt-4o-mini-2024": (9.0, 9.0)}
    assert price_for_model("gpt-4o-mini-2024-07-18", overrides) == (9.0, 9.0)
    assert price_for_model("gpt-4o-mini-2023", overrides) == (0.15, 0.60)  # falls back


def test_settings_pricing_override_dict_parses_valid_entries() -> None:
    settings = Settings(pricing_overrides="gpt-4o:1.0:2.0, my-model:0.5:1.5")
    assert settings.pricing_override_dict == {
        "gpt-4o": (1.0, 2.0),
        "my-model": (0.5, 1.5),
    }


def test_settings_pricing_override_dict_skips_malformed_entries() -> None:
    settings = Settings(
        pricing_overrides="gpt-4o:1.0:oops, no-colons-here, gpt-4o:1.0, ok-model:0.1:0.2"
    )
    assert settings.pricing_override_dict == {"ok-model": (0.1, 0.2)}


def test_settings_pricing_override_dict_empty_by_default() -> None:
    assert Settings().pricing_override_dict == {}


# --- token estimation (streaming fallback) -----------------------------------
# estimate_tokens only runs when the provider doesn't report usage, but it still
# feeds cost_usd and the per-client budget cap. The old whitespace word count
# undercounted Latin text ~30% and collapsed to ~1 token for CJK (no spaces),
# so a Japanese/Chinese app could blow past its budget effectively unmetered.


def test_estimate_tokens_latin_uses_the_chars_per_token_rule() -> None:
    # 44 chars / 4 ≈ 11; the old word count gave 9 for this sentence.
    text = "The quick brown fox jumps over the lazy dog."
    assert estimate_tokens(text) == round(len(text) / 4)


def test_estimate_tokens_counts_cjk_per_character() -> None:
    # No spaces -> the old estimate returned 1. Now each kana/kanji ~1 token.
    text = "東京の天気は今日は晴れです。"  # 14 chars
    est = estimate_tokens(text)
    assert est >= len(text) - 1  # ~1 token/char, allowing rounding
    # And emphatically not the 1 token the word count produced.
    assert est > len(text.split())


def test_estimate_tokens_mixed_script_adds_both_classes() -> None:
    # Latin at 1/4, CJK at 1 — a mixed reply must count both, not whichever
    # class the whitespace happened to land in.
    text = "Error: レート制限に達しました"
    cjk = sum(1 for ch in text if "぀" <= ch <= "鿿")
    latin = len(text) - cjk
    assert estimate_tokens(text) == max(1, round(cjk + latin / 4))


def test_estimate_tokens_errs_high_for_cjk_never_catastrophically_low() -> None:
    # The safe direction for a budget cap: a long CJK reply must estimate on the
    # order of its character count, not a handful of tokens.
    text = "人工知能ゲートウェイは複数のプロバイダを一つのAPIに統合します。" * 4
    est = estimate_tokens(text)
    assert est >= 0.8 * len(text)  # within the measured ~15% band, on the high side


def test_estimate_tokens_floor_and_empty() -> None:
    assert estimate_tokens("") == 1
    assert estimate_tokens(" ") == 1
    assert estimate_tokens("a") == 1


def test_cjk_estimate_changes_the_budget_math() -> None:
    # The point of the fix, in dollars: the same reply now bills ~its length in
    # tokens instead of ~one, so the cost estimate is not ~0.
    usage_old_would_be = Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    reply = "レート制限に達しましたので、しばらくお待ちください。" * 3
    tokens = estimate_tokens(reply)
    usage_now = Usage(prompt_tokens=1, completion_tokens=tokens, total_tokens=1 + tokens)
    cheap = estimate_cost("openai", "gpt-4o", usage_old_would_be)
    real = estimate_cost("openai", "gpt-4o", usage_now)
    assert real is not None and cheap is not None
    assert real > cheap * 20  # dramatically different, not a rounding nudge
