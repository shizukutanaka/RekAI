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
[ body guard ]    ── 413 if the /v1 body exceeds REKAI_MAX_BODY_BYTES
   │
   ▼
[ auth ]          ── 401 if a configured gateway key is missing/invalid
   │
   ▼
[ rate limiter ]  ── 429 (+ Retry-After) if the client (per key, else IP) exceeds its budget
   │
   ▼
[ router ]  ── picks a provider (explicit → model-prefix → default)
   │
   ▼
[ cache ]  ── hit? return immediately (cached=true)
   │ miss
   ▼
[ provider.chat() ]  ── OpenAI · Anthropic · Gemini · Ollama · Echo (uses BYOK key if present)
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

## Retry & fallback / failover

Each target is first **retried in place** on transient failures: a **5xx**
`ProviderError` (upstream error or network timeout) is retried up to
`REKAI_RETRY_MAX_ATTEMPTS` times (default 2 — one retry; set 1 to disable) with
**exponential backoff and full jitter** — `uniform(0, min(max, base · 2ⁿ))`,
bounded by `REKAI_RETRY_BASE_DELAY_SECONDS` / `REKAI_RETRY_MAX_DELAY_SECONDS`.
Full jitter keeps concurrent clients from synchronising their retries onto a
recovering upstream. **4xx** client errors are never retried.

A **429** (upstream rate limit) is also retried, but **honours the provider's
`Retry-After`**: RekAI waits exactly that long when it is no longer than
`max_delay`; when it is longer, RekAI gives up and **passes the `Retry-After`
header through to the client** so its SDK can back off precisely (rather than
blocking the gateway). A 429 with no header falls back to jittered backoff.

Only after a target's retries are exhausted does RekAI move on. A request may
carry an ordered `fallbacks` list of `(provider, model)` targets; alternatively
a server-wide chain is set via `REKAI_FALLBACK_ENABLED` +
`REKAI_FALLBACK_TARGETS`; both 5xx and 429 trigger failover. The serving
provider is reflected in the response `provider` field and `fallback_used` is
set when a non-primary target answered. Each fallback attempt increments
`rekai_fallbacks_total`. Retry and failover apply to embeddings too.

### Provider cooldown

When a provider 429s, it is **parked** for the upstream `Retry-After`, or
`REKAI_PROVIDER_COOLDOWN_SECONDS` (default 30s) when no header was sent. While a
provider is cooling down, routing **skips it** in favour of a healthy fallback —
unless it's the only target left, in which case it's still tried. This stops
RekAI from repeatedly hammering a provider that has already asked it to back
off, and is disabled with `REKAI_PROVIDER_COOLDOWN_ENABLED=false`.

The cooldown is checked and recorded locally first (zero-latency, in-process),
and — when `REKAI_REDIS_URL` is set — also written through to and read from
Redis. So a single process still works with no external services, but in a
multi-worker or multi-node deployment a 429 seen by one worker is honoured by
the others too, instead of each worker rediscovering the rate limit on its own
first request. Without Redis, cooldowns (like the rate limiter and metrics) are
per-process.

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

## Idempotency

A client can send an `Idempotency-Key` header (a unique id, e.g. a UUID) on
`POST /v1/chat` or `/v1/embeddings`. The first call's response is stored under
that key; a repeat with the **same key returns the stored response** (with
`Idempotent-Replay: true`) without processing again — so a network blip or an
automatic client retry can't double-process. Unlike the content cache, it is
keyed by the client-supplied id (not the request body) and works even with
`"cache": false`. Keys live in the cache backend under a separate namespace for
`REKAI_IDEMPOTENCY_TTL_SECONDS` (default 24h); it is a no-op when caching is
disabled. (Streaming responses are not covered.)

## Caching

The cache key is a SHA-256 of the `(provider, model, temperature, max_tokens,
messages)` tuple, so identical requests collapse to one upstream call. Backends:

- **Redis** when `REKAI_REDIS_URL` is set (shared across processes/nodes).
- **Memory** otherwise (per-process; great for local dev and tests).
- **Null** when caching is disabled.

A client can opt a single request out with `"cache": false`.

### Semantic cache

Exact-match caching misses paraphrases ("hi" vs "hello there"), keeping hit
rates low for natural-language prompts. With `REKAI_SEMANTIC_CACHE_ENABLED=true`,
RekAI embeds each prompt (via `REKAI_SEMANTIC_CACHE_MODEL`) and reuses a stored
response when an earlier prompt's embedding is within
`REKAI_SEMANTIC_CACHE_THRESHOLD` cosine similarity (default 0.85) — the approach
from the *GPT Semantic Cache* work (arXiv:2411.05276), which reports large
reductions in upstream calls. Entries are scoped to a `(provider, model,
temperature, max_tokens)` bucket so a hit never crosses model or params, held in
a bounded process-local store (`REKAI_SEMANTIC_CACHE_MAX_ENTRIES`, FIFO). It's
opt-in: it costs one embedding call per request and only helps with a real
embeddings model (the keyless `echo` embeddings are hash-based, not semantic).

## Request context & observability

The outermost middleware assigns each request an `X-Request-ID` (or propagates a
client-supplied one), records latency, and logs an access line
(`METHOD path -> status Nms id=...`) under the `rekai.access` logger. Set
`REKAI_LOG_FORMAT=json` for structured one-object-per-line logs (the access
record then carries `method`/`path`/`status`/`duration_ms`/`request_id` fields).
Every response also carries `X-RekAI-Version` (the gateway version that served
it), exposed to browser JS like the other custom headers. Both
`X-Request-ID` and `X-Response-Time-Ms` are returned on every response,
including errors, so requests can be traced end to end. RekAI also speaks **W3C
Trace Context**: an incoming `traceparent` is parsed and its `trace_id` is
continued (RekAI emits a new span id) — or a fresh trace is started — returned as
a `traceparent` response header and attached to the structured access log as
`trace_id`, so RekAI slots into an OpenTelemetry-traced system without the SDK.
`/v1/*` responses also
carry `X-RateLimit-Limit`/`X-RateLimit-Remaining`, and a 429 adds `Retry-After`
(seconds until the client's bucket refills a token). CORS is the outermost
middleware and exposes these custom headers (`Access-Control-Expose-Headers`),
so even a short-circuit 429 is readable by browser JS; preflight `OPTIONS`
requests don't consume rate-limit budget.

Counters live in memory for a fast, lock-protected request path (the standard
per-instance model for Prometheus `/metrics`). Alongside requests, cache
hits/misses, errors, tokens and cost, RekAI tracks `fallbacks_total`,
`retries_total` (transient failures retried in place) and `cooldowns_total`
(providers parked after a 429), so the resilience machinery is observable. When
`REKAI_REDIS_URL` is set, they are **persisted write-behind**: a baseline is
loaded on startup and the snapshot is flushed to Redis periodically and on
shutdown, so `/v1/usage` totals survive restarts. Without Redis the store is a
no-op.

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
report `null`. Embeddings responses carry `cost_usd` too (input-only — the
`text-embedding-3-*` / `ada-002` models are priced). Cumulative cost is exposed at `/v1/usage` and `/metrics`
(`rekai_cost_usd_total`). Prices are approximate and meant for budgeting, not
billing — extend or override them with `pricing.register_price()`. `/v1/models`
also reports each model's `pricing` (`input_per_1m`/`output_per_1m`, or `null`
when unknown), so clients can build cost UIs without hardcoding rates.

## Guardrails

With `REKAI_GUARDRAILS_ENABLED=true`, RekAI scans the **user** messages of a
chat / chat-stream request for common prompt-injection / jailbreak phrasings
("ignore previous instructions", "reveal your system prompt", "developer mode
enabled", …) before calling a provider. `REKAI_GUARDRAILS_ACTION=block` (default)
rejects a flagged request with `403 guardrail_blocked`; `flag` lets it through
with an `X-Guardrail-Flag: <pattern>` header so a caller can decide. This is a
**heuristic first layer** (OWASP LLM01), not a security boundary — obfuscated or
encoded attacks evade regexes (arXiv:2504.11168), so keep tools least-privileged
and add a classifier-based guardrail where assurance matters. Off by default.

## Gateway authentication

Two distinct keys are in play. The **gateway** key authenticates the *client to
RekAI*: set `REKAI_API_KEYS` (comma-separated) and `/v1/*` then requires
`Authorization: Bearer <key>`, compared in constant time; missing/invalid →
`401` with `WWW-Authenticate: Bearer`. With no keys configured the gateway is
open (the default). System endpoints (`/health`, `/metrics`, `/`, `/docs`) stay
open for liveness probes and scraping. This is separate from **BYOK** below,
which is the *upstream provider* key.

Rate limiting is **per tenant**: when authenticated, the bucket is keyed by the
API key (a non-reversible `key:<hash>` id, also attached to the structured
access log as `client`) rather than the client IP, so one tenant's traffic can't
exhaust another's budget. Without auth it falls back to the client IP.

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
overriding `embed()`. `/v1/models` tags every entry with a `type`
(`chat` or `embedding`) and lists embedding models alongside chat ones —
providers advertise them via `list_embedding_models()`, so clients (and the web
**Embeddings** page) can discover and route to the right one.

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
`REKAI_CUSTOM_NAME`, `REKAI_CUSTOM_API_KEY`, `REKAI_CUSTOM_MODELS`,
`REKAI_CUSTOM_EMBEDDING_MODELS`) to register `OpenAICompatibleProvider`, which
reuses the OpenAI implementation (including accurate streaming usage and the
`/embeddings` call) pointed at that endpoint. Select it with `provider="<name>"`
or `REKAI_DEFAULT_PROVIDER`; any configured embedding models are advertised in
`/v1/models` with `type="embedding"`.
