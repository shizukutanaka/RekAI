"""Single source of truth for model → provider → price → type.

This consolidates three things that used to be maintained separately and drifted
out of sync:

* ``router.py``'s prefix → provider routing rules,
* ``pricing.py``'s prefix → price table, and
* each provider's ``list_models()`` / ``list_embedding_models()`` lists.

``pricing`` builds its table from :func:`price_table`, ``router`` reads
:data:`PROVIDER_PREFIXES`, and each built-in provider advertises
:func:`advertised_models`. A single test (``test_models.py``) asserts the whole
registry is internally consistent — every advertised model both routes back to
the provider that advertises it and (for chat) has a price — so a model can't be
added to one surface and forgotten on the others.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelSpec:
    """One priced/advertised model family.

    ``prefix`` is matched with ``str.startswith`` for both pricing (longest
    prefix wins) and the consistency check, so a dated id like
    ``claude-opus-4-8`` is covered by the ``claude-opus`` prefix. ``advertised``
    lists the concrete ids surfaced on ``/v1/models`` (empty = priced but not
    advertised, e.g. a legacy ``gpt-4``).
    """

    prefix: str
    provider: str
    kind: str  # "chat" | "embedding"
    price: tuple[float, float] | None = None  # (input, output) USD per 1M tokens
    advertised: tuple[str, ...] = field(default_factory=tuple)


# Ordered so /v1/models advertises models in this order. Pricing uses longest-
# prefix matching (order-independent); routing uses PROVIDER_PREFIXES below.
MODEL_SPECS: tuple[ModelSpec, ...] = (
    # --- OpenAI chat ---
    ModelSpec("gpt-4o", "openai", "chat", (2.50, 10.00), ("gpt-4o",)),
    ModelSpec("gpt-4o-mini", "openai", "chat", (0.15, 0.60), ("gpt-4o-mini",)),
    ModelSpec("gpt-4-turbo", "openai", "chat", (10.00, 30.00), ("gpt-4-turbo",)),
    ModelSpec("gpt-4", "openai", "chat", (30.00, 60.00)),  # priced, not advertised
    ModelSpec("gpt-3.5-turbo", "openai", "chat", (0.50, 1.50), ("gpt-3.5-turbo",)),
    ModelSpec("o1", "openai", "chat", (15.00, 60.00), ("o1",)),
    ModelSpec("o1-mini", "openai", "chat", (1.10, 4.40), ("o1-mini",)),
    ModelSpec("o3-mini", "openai", "chat", (1.10, 4.40), ("o3-mini",)),
    # --- Anthropic chat ---
    ModelSpec("claude-opus", "anthropic", "chat", (15.00, 75.00), ("claude-opus-4-8",)),
    ModelSpec("claude-sonnet", "anthropic", "chat", (3.00, 15.00), ("claude-sonnet-4-6",)),
    ModelSpec("claude-haiku", "anthropic", "chat", (0.80, 4.00), ("claude-haiku-4-5",)),
    # --- Google Gemini chat ---
    ModelSpec("gemini-2.5-pro", "gemini", "chat", (1.25, 10.00), ("gemini-2.5-pro",)),
    ModelSpec("gemini-2.0-flash", "gemini", "chat", (0.10, 0.40), ("gemini-2.0-flash",)),
    ModelSpec("gemini-1.5-pro", "gemini", "chat", (1.25, 5.00), ("gemini-1.5-pro",)),
    ModelSpec("gemini-1.5-flash", "gemini", "chat", (0.075, 0.30), ("gemini-1.5-flash",)),
    # --- OpenAI embeddings (input-only; output price is 0) ---
    ModelSpec(
        "text-embedding-3-small", "openai", "embedding", (0.02, 0.0), ("text-embedding-3-small",)
    ),
    ModelSpec(
        "text-embedding-3-large", "openai", "embedding", (0.13, 0.0), ("text-embedding-3-large",)
    ),
    ModelSpec(
        "text-embedding-ada-002", "openai", "embedding", (0.10, 0.0), ("text-embedding-ada-002",)
    ),
    # --- Gemini embeddings (advertised but unpriced) ---
    ModelSpec("text-embedding-004", "gemini", "embedding", None, ("text-embedding-004",)),
    # --- Echo (keyless; free) ---
    ModelSpec("echo", "echo", "chat", None, ("echo",)),
    ModelSpec("echo", "echo", "embedding", None, ("echo",)),
)


# Family-level routing: broad prefix → provider, first match wins. Broader than
# MODEL_SPECS on purpose, so an unknown/unpriced model in a known family still
# routes (a new "gpt-5" → openai before it's priced or advertised). Ollama runs
# arbitrary local models, so its families route but advertise nothing static.
PROVIDER_PREFIXES: tuple[tuple[str, str], ...] = (
    ("gpt-", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("claude", "anthropic"),
    ("gemini", "gemini"),
    ("text-embedding", "openai"),
    ("llama", "ollama"),
    ("mistral", "ollama"),
    ("qwen", "ollama"),
    ("gemma", "ollama"),
    ("phi", "ollama"),
    ("echo", "echo"),
)


def provider_for_prefix(model: str) -> str | None:
    """The provider a model id routes to by family prefix, or None if unknown."""
    model_lower = model.lower()
    for prefix, provider in PROVIDER_PREFIXES:
        if model_lower.startswith(prefix):
            return provider
    return None


def price_table() -> dict[str, tuple[float, float]]:
    """The prefix → (input, output) price map derived from the registry."""
    return {spec.prefix: spec.price for spec in MODEL_SPECS if spec.price is not None}


def advertised_models(provider: str, kind: str) -> list[str]:
    """Concrete model ids a provider advertises on /v1/models for ``kind``."""
    out: list[str] = []
    for spec in MODEL_SPECS:
        if spec.provider == provider and spec.kind == kind:
            out.extend(spec.advertised)
    return out
