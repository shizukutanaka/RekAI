from rekai.providers import get_provider, provider_names, register_provider
from rekai.providers.base import Provider, ProviderResult
from rekai.providers.echo import EchoProvider
from rekai.schemas import ChatMessage, ChatRequest, Usage


def _req(content: str = "hello world") -> ChatRequest:
    return ChatRequest(model="echo", messages=[ChatMessage(role="user", content=content)])


async def test_echo_provider_echoes_last_user_message() -> None:
    result = await EchoProvider().chat(_req("ping"), api_key=None)
    assert result.content == "Echo: ping"
    assert result.usage.total_tokens > 0


async def test_echo_models() -> None:
    assert await EchoProvider().list_models(None) == ["echo"]


def test_registry_contains_builtin_providers() -> None:
    names = provider_names()
    assert {"echo", "openai", "ollama"} <= set(names)


def test_register_custom_provider() -> None:
    class Custom(Provider):
        name = "custom-test"
        requires_key = False

        async def chat(self, request, api_key) -> ProviderResult:
            return ProviderResult(content="ok", model=request.model, usage=Usage())

    register_provider(Custom())
    assert get_provider("custom-test") is not None
