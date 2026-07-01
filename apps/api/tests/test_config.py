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
    ],
)
def test_invalid_settings_are_rejected(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        Settings(**kwargs)
