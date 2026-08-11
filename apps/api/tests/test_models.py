"""The single model registry (rekai/models.py) must stay internally consistent:
routing, pricing, and the advertised /v1/models list all derive from it."""

from __future__ import annotations

from rekai import models
from rekai.config import Settings
from rekai.pricing import price_for_model
from rekai.router import resolve_provider


def test_price_table_matches_specs() -> None:
    table = models.price_table()
    for spec in models.MODEL_SPECS:
        if spec.price is not None:
            assert table[spec.prefix] == spec.price
    # Unpriced specs (echo, gemini text-embedding-004) are absent from the table.
    assert "echo" not in table
    assert "text-embedding-004" not in table


def test_every_advertised_chat_model_routes_and_prices() -> None:
    # For each provider that advertises chat models, every advertised id must
    # route back to that provider AND be priced — the guarantee that lets the
    # router, price table, and /v1/models share one source without drifting.
    settings = Settings(environment="test", default_provider="echo")
    for spec in models.MODEL_SPECS:
        if spec.kind != "chat":
            continue
        for model in spec.advertised:
            assert resolve_provider(None, model, settings) == spec.provider, (
                f"{model} advertised by {spec.provider} but routes elsewhere"
            )
            # echo is free/keyless and intentionally unpriced.
            if spec.provider != "echo":
                assert price_for_model(model) is not None, f"{model} advertised but unpriced"


def test_every_advertised_embedding_model_routes_to_its_provider() -> None:
    # The chat check above left embeddings unguarded, and they drifted:
    # `text-embedding-004` is advertised on /v1/models as a *gemini* model, but
    # the `text-embedding` -> openai family rule matched it first, so a request
    # for it was routed to OpenAI (a 401/unknown-model against the wrong
    # upstream, or a silent bill on the operator's OpenAI key). Embeddings are
    # not required to be priced — some advertised ones deliberately aren't.
    settings = Settings(environment="test", default_provider="echo")
    for spec in models.MODEL_SPECS:
        if spec.kind != "embedding":
            continue
        for model in spec.advertised:
            assert resolve_provider(None, model, settings) == spec.provider, (
                f"{model} advertised by {spec.provider} but routes elsewhere"
            )


def test_openai_embedding_family_still_routes_to_openai() -> None:
    # The gemini-specific rule must not shadow the broader OpenAI family.
    assert models.provider_for_prefix("text-embedding-004") == "gemini"
    assert models.provider_for_prefix("text-embedding-3-small") == "openai"
    assert models.provider_for_prefix("text-embedding-ada-002") == "openai"


def test_advertised_models_groups_by_provider_and_kind() -> None:
    assert models.advertised_models("openai", "chat")[0].startswith("gpt-")
    assert models.advertised_models("gemini", "chat") == [
        "gemini-2.5-pro",
        "gemini-2.0-flash",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
    ]
    assert models.advertised_models("openai", "embedding") == [
        "text-embedding-3-small",
        "text-embedding-3-large",
        "text-embedding-ada-002",
    ]
    assert models.advertised_models("gemini", "embedding") == ["text-embedding-004"]
    assert models.advertised_models("echo", "chat") == ["echo"]
    # Unknown provider or kind yields nothing.
    assert models.advertised_models("nope", "chat") == []
    assert models.advertised_models("openai", "audio") == []


def test_provider_for_prefix_routes_families() -> None:
    assert models.provider_for_prefix("gpt-4o") == "openai"
    assert models.provider_for_prefix("o1-preview") == "openai"
    assert models.provider_for_prefix("claude-opus-4-8") == "anthropic"
    assert models.provider_for_prefix("gemini-3-ultra") == "gemini"
    assert models.provider_for_prefix("llama3.1") == "ollama"
    # A brand-new model in a known family still routes even before it's priced.
    assert models.provider_for_prefix("gpt-5") == "openai"
    # Genuinely unknown -> None (caller falls back to the default provider).
    assert models.provider_for_prefix("some-unknown-model") is None
