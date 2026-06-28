"""Shared test fixtures."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rekai.config import Settings
from rekai.main import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        default_provider="echo",
        cache_enabled=True,
        redis_url=None,
        rate_limit_enabled=False,
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))
