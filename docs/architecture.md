# Architecture

RekAI is a small, modular AI gateway. This document describes how a request
flows through the system and how the pieces fit together.

## Request lifecycle

```
POST /v1/chat
   │
   ▼
[ request ctx ]   ── assigns/propagates X-Request-ID, times the request
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
| `rekai/pricing.py`      | Per-model price table + cost estimation                   |
| `rekai/rate_limit.py`   | Per-client token bucket                                   |
| `rekai/security.py`     | Optional key encryption helpers, key masking              |
| `rekai/metrics.py`      | Prometheus-style counters                                 |
| `rekai/config.py`       | Environment-driven settings                               |
| `rekai/schemas.py`      | Pydantic models = the public OpenAPI contract             |

## Routing rules

1. If the request specifies `provider`, that wins.
2. Otherwise the model name is matched against known prefixes
   (`gpt-*`, `o1*`, `o3*` → OpenAI; `claude*` → Anthropic; `gemini*` → Gemini;
   `llama*`, `mistral*`, `qwen*`, `gemma*`, `phi*` → Ollama; `echo` → Echo).
3. Otherwise the configured `REKAI_DEFAULT_PROVIDER` is used.

## Fallback / failover

A request may carry an ordered `fallbacks` list of `(provider, model)` targets;
alternatively a server-wide chain is set via `REKAI_FALLBACK_ENABLED` +
`REKAI_FALLBACK_TARGETS`. When an attempt raises a **5xx** `ProviderError`
(upstream or network failure), RekAI moves to the next target. **4xx** client
errors (bad request, missing BYOK key) are terminal and never trigger a
fallback. The serving provider is reflected in the response `provider` field and
`fallback_used` is set when a non-primary target answered. Each fallback attempt
increments `rekai_fallbacks_total`.

## Streaming

`POST /v1/chat/stream` returns `text/event-stream`. Each `Provider` implements
`stream()`; the base class falls back to a single `chat()` call so every
provider works on the streaming path even without native support. The endpoint
emits `data: {"delta": "..."}` events, then a final
`data: {"usage": {...}, "cost_usd": ..., "estimated": true}` summary, then a
terminating `data: [DONE]`. Providers expose `stream_events()` yielding text
deltas and an optional final provider-reported usage; when present it is used
verbatim (`estimated: false`) — all five providers do this (echo exact; OpenAI
via `stream_options`; Anthropic from `message_start`/`message_delta`; Gemini from
`usageMetadata`; Ollama from the final chunk). Otherwise usage is estimated from
the streamed text (`estimated: true`).
Either way it is recorded into `/v1/usage` and `/metrics` like non-streamed
requests. Errors are delivered as a
`data: {"error": ...}` event rather than an HTTP status, since the stream has
already started. Streamed responses are not cached.

## Caching

The cache key is a SHA-256 of the `(provider, model, temperature, max_tokens,
messages)` tuple, so identical requests collapse to one upstream call. Backends:

- **Redis** when `REKAI_REDIS_URL` is set (shared across processes/nodes).
- **Memory** otherwise (per-process; great for local dev and tests).
- **Null** when caching is disabled.

A client can opt a single request out with `"cache": false`.

## Request context & observability

The outermost middleware assigns each request an `X-Request-ID` (or propagates a
client-supplied one), records latency, and logs an access line
(`METHOD path -> status Nms id=...`) under the `rekai.access` logger. Both
`X-Request-ID` and `X-Response-Time-Ms` are returned on every response,
including errors, so requests can be traced end to end.

Counters live in memory for a fast, lock-protected request path (the standard
per-instance model for Prometheus `/metrics`). When `REKAI_REDIS_URL` is set,
they are **persisted write-behind**: a baseline is loaded on startup and the
snapshot is flushed to Redis periodically and on shutdown, so `/v1/usage`
totals survive restarts. Without Redis the store is a no-op.

## Tool / function calling

`ChatRequest` accepts OpenAI-style `tools` and `tool_choice`, passed through to
providers that support them (OpenAI and OpenAI-compatible backends). The model's
`tool_calls` are returned on `ChatResponse`. Messages carry the round-trip
fields (`tool_calls`, `tool_call_id`, `name`) and `content` is optional, so a
full tools conversation can be replayed. Providers without tool support ignore
these fields. For **streaming**, tool calls are assembled and returned in the
final summary event's `tool_calls` for all three providers (OpenAI/Anthropic
deltas accumulated by index; Gemini `functionCall` parts collected).

Tools work natively on **Anthropic** too: OpenAI-style `tools`/`tool_choice` are
translated to Anthropic's `tools`/`input_schema`/`tool_choice`, assistant
`tool_calls` and `tool` results round-trip to Anthropic `tool_use`/`tool_result`
content blocks, and Anthropic's `tool_use` responses map back to OpenAI-style
`tool_calls` — so a tools conversation is portable across OpenAI and Anthropic.
**Gemini** is supported the same way (`functionDeclarations`/`functionCall`/
`functionResponse` + `toolConfig`), so tool calling works uniformly across all
three major cloud providers through one OpenAI-style API.

## Cost estimation

Each non-streamed response carries an approximate `cost_usd`, computed by
`rekai/pricing.py` from a per-model price table (`(input, output)` USD per 1M
tokens). Free/local providers (`echo`, `ollama`) report `0.0`; unpriced models
report `null`. Cumulative cost is exposed at `/v1/usage` and `/metrics`
(`rekai_cost_usd_total`). Prices are approximate and meant for budgeting, not
billing — extend or override them with `pricing.register_price()`.

## BYOK

Provider keys arrive per request via the `X-Provider-Key` header. They are
passed straight to the provider call and never logged, cached, or persisted. A
server-side default key (e.g. `REKAI_OPENAI_API_KEY`) is used only when no BYOK
header is present.

### Readiness

`/health` reports `provider_status` per provider: `ready` (usable now — keyless,
or a server-side key is configured) or `byok_only` (needs an `X-Provider-Key`).
Key-requiring providers override `server_key_configured()` to check their key.

## Embeddings

`POST /v1/embeddings` mirrors the chat path: route → cache → `provider.embed()`.
It accepts a string or list of strings, returns one vector per input, and is
cached (keyed by provider/model/inputs). Echo returns deterministic hash-based
vectors so the endpoint works with no key; OpenAI and OpenAI-compatible backends
call the real `/embeddings` API, **Ollama** calls its local `/api/embed`
(keyless, e.g. `nomic-embed-text`), and **Gemini** uses `:batchEmbedContents`
(e.g. `text-embedding-004`, via `provider="gemini"`). Providers opt in by
overriding `embed()`.

## Adding a provider

1. Subclass `rekai.providers.base.Provider` and implement `chat()`.
2. Register it in `rekai/providers/registry.py` (or at runtime with
   `register_provider`).
3. Optionally add routing prefixes in `rekai/router.py` and override
   `server_key_configured()` if it needs a key.

That's the entire surface area — see `rekai/providers/echo.py` for the smallest
working example.

### OpenAI-compatible backends

Many providers (Groq, Together, OpenRouter, Mistral, vLLM, LM Studio, …) speak
the OpenAI `/chat/completions` API. Set `REKAI_CUSTOM_BASE_URL` (plus optional
`REKAI_CUSTOM_NAME`, `REKAI_CUSTOM_API_KEY`, `REKAI_CUSTOM_MODELS`) to register
`OpenAICompatibleProvider`, which reuses the OpenAI implementation (including
accurate streaming usage) pointed at that endpoint. Select it with
`provider="<name>"` or `REKAI_DEFAULT_PROVIDER`.
