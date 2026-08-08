import pytest

from rekai.config import Settings
from rekai.providers.base import ProviderError
from rekai.router import resolve_provider_name, select_provider
from rekai.schemas import ChatMessage, ChatRequest


def _req(**kwargs) -> ChatRequest:
    kwargs.setdefault("messages", [ChatMessage(role="user", content="hi")])
    return ChatRequest(**kwargs)


@pytest.fixture
def settings() -> Settings:
    return Settings(default_provider="echo")


def test_explicit_provider_wins(settings: Settings) -> None:
    assert resolve_provider_name(_req(model="gpt-4o", provider="ollama"), settings) == "ollama"


@pytest.mark.parametrize(
    "model,expected",
    [
        ("gpt-4o-mini", "openai"),
        ("o1-preview", "openai"),
        ("claude-sonnet-4-6", "anthropic"),
        ("gemini-1.5-pro", "gemini"),
        ("llama3.1", "ollama"),
        ("mistral", "ollama"),
        ("echo", "echo"),
    ],
)
def test_prefix_routing(settings: Settings, model: str, expected: str) -> None:
    assert resolve_provider_name(_req(model=model), settings) == expected


def test_falls_back_to_default(settings: Settings) -> None:
    assert resolve_provider_name(_req(model="unknown-model"), settings) == "echo"


def test_select_unknown_provider_raises(settings: Settings) -> None:
    with pytest.raises(ProviderError):
        select_provider(_req(model="x", provider="nope"), settings)


# --- REKAI_ALLOWED_PROVIDERS -------------------------------------------------
# A client steers provider choice three ways: an explicit `provider`, the model
# name's prefix, and request-level `fallbacks`. The allowlist governs all three
# so an operator with server-side keys for several providers can say which ones
# tenants may actually spend on.


def _allowlisted() -> Settings:
    return Settings(default_provider="echo", allowed_providers="openai")


def test_no_allowlist_permits_every_provider() -> None:
    settings = Settings(default_provider="echo")
    assert select_provider(_req(model="x", provider="anthropic"), settings)[0] == "anthropic"


def test_allowlist_rejects_an_explicit_provider() -> None:
    with pytest.raises(ProviderError) as exc:
        select_provider(_req(model="x", provider="anthropic"), _allowlisted())
    assert exc.value.status_code == 403


def test_allowlist_rejects_provider_reached_by_model_prefix() -> None:
    # No explicit provider — "claude-*" routes to anthropic, which is off-list.
    with pytest.raises(ProviderError) as exc:
        select_provider(_req(model="claude-sonnet-4-6"), _allowlisted())
    assert exc.value.status_code == 403


def test_allowlist_permits_listed_and_default_providers() -> None:
    settings = _allowlisted()
    assert select_provider(_req(model="gpt-4o-mini"), settings)[0] == "openai"
    # default_provider is always allowed even when absent from the list, so an
    # allowlist can't lock the gateway out of its own default.
    assert select_provider(_req(model="unknown-model"), settings)[0] == "echo"
