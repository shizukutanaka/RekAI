import httpx

from rekai.providers import get_provider, provider_names, register_provider
from rekai.providers.base import Provider, ProviderResult, parse_retry_after, provider_http_error
from rekai.providers.echo import EchoProvider
from rekai.providers.openai import OpenAIProvider
from rekai.schemas import ChatMessage, ChatRequest, Usage


def test_parse_retry_after() -> None:
    assert parse_retry_after({"Retry-After": "5"}) == 5.0
    assert parse_retry_after({"retry-after": "0"}) == 0.0
    assert parse_retry_after({}) is None
    assert parse_retry_after({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None  # date form
    assert parse_retry_after({"Retry-After": "-3"}) is None


def test_provider_http_error_captures_retry_after_on_429() -> None:
    err = provider_http_error("openai", 429, "rate limited", {"Retry-After": "7"})
    assert err.status_code == 429
    assert err.retry_after == 7.0
    # 5xx is normalised to 502 and carries no retry_after.
    err5 = provider_http_error("openai", 503, "down", {"Retry-After": "7"})
    assert err5.status_code == 502
    assert err5.retry_after is None


def _req(content: str = "hello world") -> ChatRequest:
    return ChatRequest(model="echo", messages=[ChatMessage(role="user", content=content)])


async def test_echo_provider_echoes_last_user_message() -> None:
    result = await EchoProvider().chat(_req("ping"), api_key=None)
    assert result.content == "Echo: ping"
    assert result.usage.total_tokens > 0


async def test_echo_models() -> None:
    assert await EchoProvider().list_models(None) == ["echo"]


def test_keyless_provider_is_always_ready() -> None:
    # Keyless providers report ready without any server-side key.
    assert EchoProvider().server_key_configured() is True


def test_registry_contains_builtin_providers() -> None:
    names = provider_names()
    assert {"echo", "openai", "anthropic", "gemini", "ollama"} <= set(names)


def test_register_custom_provider() -> None:
    class Custom(Provider):
        name = "custom-test"
        requires_key = False

        async def chat(self, request, api_key) -> ProviderResult:
            return ProviderResult(content="ok", model=request.model, usage=Usage())

    register_provider(Custom())
    assert get_provider("custom-test") is not None


async def test_client_is_reused_across_calls_on_same_loop() -> None:
    # _client() returns a persistent httpx.AsyncClient so upstream connections
    # can be pooled instead of a fresh handshake per request.
    provider = OpenAIProvider()
    c1 = provider._client(30.0)
    c2 = provider._client(30.0)
    assert c1 is c2
    assert isinstance(c1, httpx.AsyncClient)


async def test_client_rebuilt_when_event_loop_changes() -> None:
    # The pool is bound to the loop it was created on; a client cached from a
    # prior loop must not be reused (each pytest-asyncio test gets its own loop).
    provider = OpenAIProvider()
    first = provider._client(30.0)
    # Simulate the "cached from a now-defunct loop" state the next test's loop
    # would see, without needing a second real loop.
    provider._http_client_loop = object()  # type: ignore[assignment]
    rebuilt = provider._client(30.0)
    assert rebuilt is not first
