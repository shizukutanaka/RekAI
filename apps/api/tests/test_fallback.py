"""Tests for provider fallback/failover."""

from __future__ import annotations

import pytest

from rekai.cache import NullCache
from rekai.config import Settings
from rekai.providers import register_provider
from rekai.providers.base import Provider, ProviderError, ProviderResult
from rekai.schemas import ChatMessage, ChatRequest
from rekai.service import handle_chat


class FlakyProvider(Provider):
    """Always fails with the configured status code."""

    requires_key = False

    def __init__(self, name: str, status_code: int) -> None:
        self.name = name
        self._status = status_code

    async def chat(self, request, api_key) -> ProviderResult:
        raise ProviderError(f"{self.name} boom", status_code=self._status)


@pytest.fixture(autouse=True)
def _register_flaky():
    register_provider(FlakyProvider("flaky5xx", 503))
    register_provider(FlakyProvider("flaky4xx", 400))


def _req(**kwargs) -> ChatRequest:
    kwargs.setdefault("messages", [ChatMessage(role="user", content="hi")])
    return ChatRequest(**kwargs)


def _settings(**kwargs) -> Settings:
    kwargs.setdefault("default_provider", "echo")
    return Settings(**kwargs)


async def test_request_fallback_on_5xx() -> None:
    request = _req(
        model="x",
        provider="flaky5xx",
        fallbacks=[{"provider": "echo", "model": "echo"}],
    )
    resp = await handle_chat(request, None, _settings(), NullCache())
    assert resp.provider == "echo"
    assert resp.fallback_used is True
    assert resp.content.startswith("Echo:")


async def test_no_fallback_on_4xx() -> None:
    request = _req(
        model="x",
        provider="flaky4xx",
        fallbacks=[{"provider": "echo"}],
    )
    # 4xx is a client error -> should NOT fall through; the error propagates.
    with pytest.raises(ProviderError) as exc:
        await handle_chat(request, None, _settings(), NullCache())
    assert exc.value.status_code == 400


async def test_server_default_chain_used_when_enabled() -> None:
    request = _req(model="x", provider="flaky5xx")  # no per-request fallbacks
    settings = _settings(fallback_enabled=True, fallback_targets="echo")
    resp = await handle_chat(request, None, settings, NullCache())
    assert resp.provider == "echo"
    assert resp.fallback_used is True


async def test_no_fallback_when_disabled() -> None:
    request = _req(model="x", provider="flaky5xx")
    settings = _settings(fallback_enabled=False, fallback_targets="echo")
    with pytest.raises(ProviderError):
        await handle_chat(request, None, settings, NullCache())


async def test_all_fallbacks_fail_raises_last() -> None:
    request = _req(
        model="x",
        provider="flaky5xx",
        fallbacks=[{"provider": "flaky5xx"}, {"provider": "flaky4xx"}],
    )
    # flaky5xx (503) -> flaky5xx skipped as duplicate? no, different attempt;
    # chain: flaky5xx(503) -> flaky4xx(400) which is terminal.
    with pytest.raises(ProviderError) as exc:
        await handle_chat(request, None, _settings(), NullCache())
    assert exc.value.status_code == 400


async def test_primary_success_no_fallback() -> None:
    request = _req(model="echo", fallbacks=[{"provider": "flaky5xx"}])
    resp = await handle_chat(request, None, _settings(), NullCache())
    assert resp.provider == "echo"
    assert resp.fallback_used is False
