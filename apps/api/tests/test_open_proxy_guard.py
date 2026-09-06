"""The one configuration RekAI refuses to serve: an open proxy for its own keys.

A gateway with no client auth and a server-side provider key is an
unauthenticated proxy to a paid API — anyone who can reach the port spends the
operator's money, and every request looks legitimate to the provider. Neither
half is a hazard alone: an open gateway with no server key can only serve BYOK
and `echo`, and a server key behind auth is the ordinary single-tenant setup.

`REKAI_ENVIRONMENT` carried no behavior at all before this — declared,
documented, set by compose and by ~40 tests, read nowhere.
"""

from __future__ import annotations

import pytest

from rekai.config import Settings
from rekai.main import create_app


def _settings(**kwargs) -> Settings:
    kwargs.setdefault("default_provider", "echo")
    return Settings(**kwargs)


# --- what counts as the hazard ------------------------------------------------


def test_server_key_without_auth_is_a_hazard() -> None:
    hazard = _settings(environment="development", openai_api_key="sk-server").open_proxy_hazard()

    assert hazard is not None
    # The message has to name the key that is exposed and both ways out, or the
    # operator has to go read the source to act on it.
    assert "REKAI_OPENAI_API_KEY" in hazard
    assert "REKAI_API_KEYS" in hazard
    assert "X-Provider-Key" in hazard


def test_every_server_side_key_is_named() -> None:
    hazard = _settings(
        environment="development",
        openai_api_key="sk-a",
        anthropic_api_key="sk-b",
        gemini_api_key="sk-c",
        custom_api_key="sk-d",
    ).open_proxy_hazard()

    assert hazard is not None
    for name in (
        "REKAI_OPENAI_API_KEY",
        "REKAI_ANTHROPIC_API_KEY",
        "REKAI_GEMINI_API_KEY",
        "REKAI_CUSTOM_API_KEY",
    ):
        assert name in hazard


def test_no_server_key_is_not_a_hazard() -> None:
    # The default: open gateway, BYOK only. Nothing of the operator's to spend.
    assert _settings(environment="production").open_proxy_hazard() is None


def test_static_gateway_keys_clear_the_hazard() -> None:
    assert (
        _settings(
            environment="production", openai_api_key="sk-server", api_keys="sk-rekai-1"
        ).open_proxy_hazard()
        is None
    )


def test_dynamic_keystore_clears_the_hazard() -> None:
    # The middleware treats an enabled keystore as auth-on even with an empty
    # static list, so the hazard check has to agree — otherwise it would refuse
    # a deployment that is in fact authenticated.
    assert (
        _settings(
            environment="production", openai_api_key="sk-server", dynamic_keys_enabled=True
        ).open_proxy_hazard()
        is None
    )


def test_cors_wildcard_alone_is_not_the_hazard() -> None:
    # Questioned and deliberately excluded: with auth on and no credentials, a
    # wildcard origin does not let a foreign page spend anything, and with auth
    # off it changes nothing that is not already open.
    assert (
        _settings(
            environment="production", cors_origins="*", api_keys="sk-rekai-1"
        ).open_proxy_hazard()
        is None
    )


# --- what the app does about it -----------------------------------------------


def test_production_refuses_to_start() -> None:
    with pytest.raises(RuntimeError, match="Refusing to start"):
        create_app(_settings(environment="production", openai_api_key="sk-server"))


def _captured_warnings(monkeypatch) -> list[str]:
    """Collect `rekai.access` warnings emitted during create_app.

    caplog cannot see these: create_app calls configure_logging(), which clears
    the root handlers — including the one pytest installed — before the check
    runs. Recording on the logger itself is unaffected by that.
    """
    from rekai import main

    seen: list[str] = []
    monkeypatch.setattr(
        main.access_logger,
        "warning",
        lambda msg, *args: seen.append(str(msg) % args if args else str(msg)),
    )
    return seen


def test_non_production_warns_but_starts(monkeypatch) -> None:
    # An upgrade must not break the default deployment; it must not be silent
    # about it either.
    warnings = _captured_warnings(monkeypatch)

    app = create_app(_settings(environment="development", openai_api_key="sk-server"))

    assert app is not None
    assert any("REKAI_OPENAI_API_KEY" in message for message in warnings)


def test_safe_config_starts_silently(monkeypatch) -> None:
    warnings = _captured_warnings(monkeypatch)

    create_app(_settings(environment="production", api_keys="sk-rekai-1"))

    assert not any("REKAI_OPENAI_API_KEY" in message for message in warnings)
