"""Fail-fast validation of settings (bad config errors at startup, not silently)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rekai.config import Settings


def test_valid_settings_load() -> None:
    s = Settings(log_format="json", guardrails_action="flag", semantic_cache_threshold=0.9)
    assert s.log_format == "json"
    assert s.guardrails_action == "flag"
    assert s.semantic_cache_threshold == 0.9


@pytest.mark.parametrize(
    "kwargs",
    [
        {"log_format": "yaml"},  # not text|json
        {"guardrails_action": "reject"},  # not block|flag
        {"semantic_cache_threshold": 1.5},  # cosine is in [0, 1]
        {"semantic_cache_threshold": -0.1},
        {"retry_max_attempts": 0},  # must be >= 1
        {"rate_limit_requests": 0},
        {"max_body_bytes": -1},
        {"provider_cooldown_seconds": -5},
        {"client_budget_window_seconds": 0},
    ],
)
def test_invalid_settings_are_rejected(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        Settings(**kwargs)


def test_client_budget_window_seconds_defaults_to_none() -> None:
    assert Settings().client_budget_window_seconds is None


def test_semantic_threshold_overrides_longest_prefix_wins() -> None:
    s = Settings(
        semantic_cache_threshold=0.85,
        semantic_cache_thresholds="gpt:0.9, gpt-4o:0.92, echo:0.5",
    )
    assert s.semantic_cache_threshold_for("gpt-4o-mini") == 0.92
    assert s.semantic_cache_threshold_for("gpt-3.5") == 0.9
    assert s.semantic_cache_threshold_for("echo") == 0.5
    assert s.semantic_cache_threshold_for("claude-sonnet") == 0.85


def test_semantic_threshold_overrides_skip_malformed() -> None:
    s = Settings(semantic_cache_thresholds="no-colon, :0.9, x:notafloat, y:1.5, z:nan, ok:0.7")
    assert s.semantic_cache_threshold_overrides == {"ok": 0.7}
