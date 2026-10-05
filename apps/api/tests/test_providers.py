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


async def test_listed_chat_models_are_priced_and_routable() -> None:
    # Invariant: every chat model a provider advertises via list_models() must
    # have a price in the table AND route back to that same provider. Otherwise
    # /v1/models surfaces a model with null cost or one RekAI routes elsewhere
    # (the o1/o3 and gemini-2.5-pro gap this test was added to lock down).
    from rekai.config import Settings
    from rekai.pricing import price_for_model
    from rekai.providers.gemini import GeminiProvider
    from rekai.providers.openai import OpenAIProvider
    from rekai.router import resolve_provider

    settings = Settings(environment="test", default_provider="echo")
    for provider_name, provider in [("openai", OpenAIProvider()), ("gemini", GeminiProvider())]:
        for model in await provider.list_models(None):
            assert price_for_model(model) is not None, f"{model} advertised but unpriced"
            assert resolve_provider(None, model, settings) == provider_name, (
                f"{model} advertised by {provider_name} but routes elsewhere"
            )


def test_keyless_provider_is_always_ready() -> None:
    # Keyless providers report ready without any server-side key.
    assert EchoProvider().server_key_configured() is True


def test_registry_contains_builtin_providers() -> None:
    names = provider_names()
    assert {"echo", "openai", "anthropic", "gemini", "ollama"} <= set(names)


async def test_providers_see_the_request_scoped_settings() -> None:
    # O-1: provider code calls current_settings(), which resolves to the
    # Settings the service layer bound for this request — not the env-cached
    # get_settings() singleton. Before the binding existed, a test had no way
    # to reach a provider with anything but env configuration.
    from rekai.cache import NullCache
    from rekai.config import Settings, current_settings
    from rekai.schemas import ChatMessage, ChatRequest
    from rekai.service import handle_chat

    class Probe(Provider):
        name = "settings-probe"
        requires_key = False
        seen: Settings | None = None

        async def chat(self, request, api_key) -> ProviderResult:
            self.seen = current_settings()
            return ProviderResult(content="ok", model=request.model)

    probe = Probe()
    register_provider(probe)
    settings = Settings(environment="test", default_provider="echo", retry_max_attempts=1)
    resp = await handle_chat(
        ChatRequest(
            model="x",
            provider="settings-probe",
            messages=[ChatMessage(role="user", content="hi")],
        ),
        None,
        settings,
        NullCache(),
    )
    assert resp.provider == "settings-probe"
    assert probe.seen is settings


def test_providers_fall_back_to_env_settings_outside_a_request() -> None:
    # Unbound contexts (a provider invoked directly, import time) still read the
    # env-cached Settings — the pre-O-1 behavior, preserved as the default.
    from rekai.config import current_settings, get_settings

    assert current_settings() is get_settings()


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


async def test_client_rebuilt_when_timeout_changes() -> None:
    # A changed request_timeout_seconds (e.g. re-running create_app with new
    # settings) must take effect: the cached client is rebuilt with the new
    # timeout rather than frozen at the first value seen.
    provider = OpenAIProvider()
    first = provider._client(30.0)
    same = provider._client(30.0)
    assert same is first  # unchanged timeout reuses the pooled client
    rebuilt = provider._client(5.0)
    assert rebuilt is not first
    assert rebuilt.timeout.read == 5.0


async def test_client_connect_timeout_stays_fail_fast() -> None:
    # The connect phase caps at 5s (httpx's default) regardless of the read
    # budget — a black-holed host must fail fast so the retry/fallback loop
    # keeps the rest of the request timeout, not its full 60s.
    provider = OpenAIProvider()
    client = provider._client(30.0)
    assert client.timeout.connect == 5.0
    assert client.timeout.read == 30.0
    # A read budget under 5s shrinks connect with it.
    short = OpenAIProvider()._client(3.0)
    assert short.timeout.connect == 3.0


async def test_aclose_closes_the_pooled_client() -> None:
    # Shutdown teardown: the pooled client must be closed and the slot cleared
    # so a later _client() call builds fresh rather than reusing a dead pool.
    provider = OpenAIProvider()
    client = provider._client(30.0)
    await provider.aclose()
    assert client.is_closed
    assert provider._http_client is None
    rebuilt = provider._client(30.0)
    assert rebuilt is not client and not rebuilt.is_closed


async def test_aclose_without_client_is_a_noop() -> None:
    provider = EchoProvider()
    await provider.aclose()  # never built a client — must not raise
    assert provider._http_client is None


async def test_lifespan_shutdown_closes_provider_clients() -> None:
    # The app's lifespan teardown drains every registered provider's pool.
    from fastapi.testclient import TestClient

    from rekai.config import Settings
    from rekai.main import create_app

    provider = OpenAIProvider()
    register_provider(provider)
    provider._client(30.0)
    with TestClient(create_app(Settings(environment="test", default_provider="echo"))):
        pass
    assert provider._http_client is None
