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
