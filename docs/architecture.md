# Architecture

RekAI is a small, modular AI gateway. This document describes how a request
flows through the system and how the pieces fit together.

## Request lifecycle

```
POST /v1/chat
   │
   ▼
[ rate limiter ]  ── 429 if the client exceeds its budget
   │
   ▼
[ router ]  ── picks a provider (explicit → model-prefix → default)
   │
   ▼
[ cache ]  ── hit? return immediately (cached=true)
   │ miss
   ▼
[ provider.chat() ]  ── OpenAI · Ollama · Echo (uses BYOK key if present)
   │
   ▼
[ cache.set() ] + [ metrics ]  ── store response, record tokens
   │
   ▼
ChatResponse
```

## Components

| Module                  | Responsibility                                            |
|-------------------------|-----------------------------------------------------------|
| `rekai/main.py`         | FastAPI app, routes, middleware, error handling           |
| `rekai/router.py`       | Decide which provider handles a request                   |
| `rekai/service.py`      | Orchestrate route → cache → provider → cache               |
| `rekai/cache.py`        | Cache key + Redis/memory/null backends                    |
| `rekai/providers/`      | Provider abstraction and concrete backends                |
| `rekai/rate_limit.py`   | Per-client token bucket                                   |
| `rekai/security.py`     | Optional key encryption helpers, key masking              |
| `rekai/metrics.py`      | Prometheus-style counters                                 |
| `rekai/config.py`       | Environment-driven settings                               |
| `rekai/schemas.py`      | Pydantic models = the public OpenAPI contract             |

## Routing rules

1. If the request specifies `provider`, that wins.
2. Otherwise the model name is matched against known prefixes
   (`gpt-*`, `o1*`, `o3*` → OpenAI; `llama*`, `mistral*`, `qwen*`, `gemma*`,
   `phi*` → Ollama; `echo` → Echo).
3. Otherwise the configured `REKAI_DEFAULT_PROVIDER` is used.

## Caching

The cache key is a SHA-256 of the `(provider, model, temperature, max_tokens,
messages)` tuple, so identical requests collapse to one upstream call. Backends:

- **Redis** when `REKAI_REDIS_URL` is set (shared across processes/nodes).
- **Memory** otherwise (per-process; great for local dev and tests).
- **Null** when caching is disabled.

A client can opt a single request out with `"cache": false`.

## BYOK

Provider keys arrive per request via the `X-Provider-Key` header. They are
passed straight to the provider call and never logged, cached, or persisted. A
server-side default key (e.g. `REKAI_OPENAI_API_KEY`) is used only when no BYOK
header is present.

## Adding a provider

1. Subclass `rekai.providers.base.Provider` and implement `chat()`.
2. Register it in `rekai/providers/registry.py` (or at runtime with
   `register_provider`).
3. Optionally add routing prefixes in `rekai/router.py`.

That's the entire surface area — see `rekai/providers/echo.py` for the smallest
working example.
