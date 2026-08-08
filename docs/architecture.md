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
[ concurrency ]   ── 429 (+ Retry-After) if REKAI_MAX_CONCURRENT_REQUESTS are already in flight
   │
   ▼
[ auth ]          ── 401 if a configured gateway key is missing/invalid
   │
   ▼
[ rate limiter ]  ── 429 (+ Retry-After) if the client (per key, else IP) exceeds its budget
   │
   ▼
[ router ]  ── picks a provider (explicit → model-prefix → default, gated by REKAI_ALLOWED_PROVIDERS)
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

These prefixes, the price table, and each provider's advertised `/v1/models`
list all derive from a single registry — `rekai/models.py` — so they can't drift
apart (a `test_models.py` invariant asserts every advertised model routes back to
its provider and, for chat, is priced).

**Which providers a request may reach** is an operator decision, not a client
one. `REKAI_ALLOWED_PROVIDERS` (comma-separated; empty = all, the default)
restricts every client-steered path through `router.ensure_allowed`: an explicit
`provider`, the provider a model name routes to, and request-level `fallbacks`.
Off-list targets get a **403**. `REKAI_DEFAULT_PROVIDER` is always allowed, so an
allowlist can't lock the gateway out of its own default. Without this, an
operator holding server-side keys for several providers had no way to say which
ones tenants may spend on — any authenticated caller could name any of them and
bill the operator's key. Note this is an *authorization* control, not an SSRF
one: provider names have always resolved against a fixed registry, so there is
no URL a client can inject.

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

A 429 is an explicit "back off" signal and parks a provider immediately. A bare
5xx isn't — one bad request shouldn't take a whole provider out of rotation —
so `REKAI_CIRCUIT_BREAKER_THRESHOLD` (default 3) requires that many consecutive
5xx failures *across separate requests* before parking it the same way. A
single success (from any request) resets the count to zero. This is a
lightweight circuit breaker: without it, a provider stuck returning 500s paid
for a full retry-with-backoff cycle on every single request before falling
over to a fallback; with it, requests after the threshold skip straight past
it. The failure counter (`rekai/circuit_breaker.py`) is process-local by
design — unlike the cooldown itself, it doesn't need cross-worker sharing,
since each worker converges to the same parked state within a few requests
anyway. Covers both the fallback-chain path (`/v1/chat`, `/v1/chat/stream`
falls through the same way) and the streaming path (which has no fallback
chain of its own to reroute to, but still benefits later non-streaming
requests that check the cooldown).

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

## OpenAI compatibility

`POST /v1/chat/completions` is a drop-in for OpenAI's ChatCompletions API, so an
OpenAI SDK (or LangChain, etc.) pointed at RekAI's base URL works unmodified. It
is a thin translation layer (`rekai/openai_compat.py`, pure functions, no I/O)
over the same internal pipeline as `/v1/chat` — the request is translated to the
internal `ChatRequest`, run through the shared `_run_chat` / `handle_chat_stream`
core (so routing, cache, retries, fallback, budgets, per-client accounting, and
`Idempotency-Key` all apply identically), and the result translated back to the
OpenAI shape (`chat.completion` / streamed `chat.completion.chunk`). Because the
path is under `/v1/`, the same auth/rate-limit/budget/body-size middleware
covers it.

Two RekAI extensions select a provider (OpenAI's schema has no provider field):
an optional `provider` body field, or an OpenRouter-style `"<provider>/<model>"`
model string (split only when the prefix is a *registered* provider, so a custom
backend's own slash-containing model ids are left intact). Unknown OpenAI tuning
params are tolerated and ignored; `n > 1` is a 400; errors use OpenAI's error
envelope. `response_format` (JSON mode / `json_schema`) is accepted on both this
endpoint and native `/v1/chat`, and forwarded to providers that support it
(OpenAI/OpenAI-compatible natively, Gemini best-effort via `responseMimeType`/
`responseSchema`; Anthropic and Ollama ignore it). It is part of the cache key,
so a JSON-mode request and a plain one never collide.

## Idempotency

A client can send an `Idempotency-Key` header (a unique id, e.g. a UUID) on
`POST /v1/chat`, `/v1/embeddings`, or the non-streaming path of the
OpenAI-compatible `/v1/chat/completions`. The first call's response is stored
under that key; a repeat with the **same key returns the stored response**
(with `Idempotent-Replay: true`) without processing again — so a network blip
or an automatic client retry can't double-process. Unlike the content cache, it
is keyed by the client-supplied id (not the request body) and works even with
`"cache": false`. Keys live in the cache backend under a separate namespace for
`REKAI_IDEMPOTENCY_TTL_SECONDS` (default 24h); it is a no-op when caching is
disabled.

Records are **scoped per client** — the store key hashes the caller's client id
(the masked API-key id under gateway auth, else the client IP) together with the
header value. Idempotency keys are caller-chosen and collide readily (`req-1`),
so a global namespace would let one tenant replay another's stored response, or
claim the in-progress sentinel first and 409 them out of their own key. Stripe
scopes idempotency keys per API key for the same reason. A consequence worth
knowing: with gateway auth off, the scope is the client IP, so callers behind one
NAT share a namespace — use `REKAI_API_KEYS` for real tenant isolation.

**Streaming responses are not covered** — neither `POST /v1/chat/stream` nor
`stream: true` on `/v1/chat/completions` accept an `Idempotency-Key`. A
retried streaming request always re-runs. A client that needs replay-safety
on retry should use the non-streaming endpoints, or de-duplicate on its own
side for streamed requests.

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
reductions in upstream calls.

Unlike the exact cache, **a semantic hit answers a prompt that was never sent**,
so everything about it is deliberately conservative:

- **Bucket.** `cache.semantic_bucket` is `cache_key`'s payload *minus*
  `messages` — provider, model, temperature, `max_tokens`, `tools`,
  `tool_choice`, `response_format`, `cache_control` — plus the **client id**.
  The message text is what the embedding compares; everything else must match
  exactly. (Cross-tenant reuse would hand tenant B an answer to a question only
  tenant A asked. The exact cache can share freely because a hit there requires
  B to have sent the identical prompt itself — so the semantic cache trades hit
  rate for isolation, on purpose.)
- **Bounds.** Process-local, FIFO, capped by `REKAI_SEMANTIC_CACHE_MAX_ENTRIES`,
  and entries expire after `REKAI_CACHE_TTL_SECONDS` like the exact cache's do.
- **Cost.** The embedding call is a real upstream call the caller never asked
  for, billed to the operator's key — its tokens and cost are recorded, so
  `/v1/usage` doesn't make the feature look cheaper than it is.
- **Embedding quality.** `REKAI_SEMANTIC_CACHE_MODEL` has **no default**:
  enabling the cache without naming a model is refused at startup. The threshold
  is meaningless unless the embedding is genuinely semantic — the keyless `echo`
  embeddings are a 16-dimension SHA-256 slice, so every vector sits in the
  positive orthant, *unrelated* prompts sit around 0.78 cosine, and ~12% of
  random pairs clear the 0.85 default (measured; e.g.
  `cos("what is 2+2", "translate to french") = 0.906`). Configuring it anyway is
  allowed for tests but logs a loud startup warning.

## Request context & observability

The outermost middleware assigns each request an `X-Request-ID` (or propagates a
client-supplied one), records latency, and logs an access line
(`METHOD path -> status Nms id=...`) under the `rekai.access` logger. Set
`REKAI_LOG_FORMAT=json` for structured one-object-per-line logs (the access
record then carries `method`/`path`/`status`/`duration_ms`/`request_id` fields).
Chat and embeddings access lines additionally carry the **OpenTelemetry GenAI
semantic-convention** attributes — `gen_ai.operation.name`,
`gen_ai.provider.name`, `gen_ai.request.model`, and
`gen_ai.usage.input_tokens`/`output_tokens` — so the JSON logs feed a GenAI
observability dashboard (Datadog, Grafana, …) directly, without a full OTel SDK.
Streaming lines carry the model/provider fields (set before the body streams)
but not token usage, since the access line is emitted before the stream is
fully consumed.
Every response also carries `X-RekAI-Version` (the gateway version that served
it), exposed to browser JS like the other custom headers. Both
`X-Request-ID` and `X-Response-Time-Ms` are returned on every response,
including errors, so requests can be traced end to end. RekAI also speaks **W3C
Trace Context**: an incoming `traceparent` is parsed and its `trace_id` is
continued (RekAI emits a new span id) — or a fresh trace is started — returned as
a `traceparent` response header and attached to the structured access log as
`trace_id`, so RekAI slots into an OpenTelemetry-traced system without the SDK.

The same `trace_id` is also forwarded to the **upstream provider** — every
provider's outbound HTTP call (OpenAI, Anthropic, Gemini, Ollama, and any
OpenAI-compatible backend; chat, streaming, and embeddings) carries its own
`traceparent`, continuing the request's trace with a fresh span id rather than
reusing the one already returned to the client. Previously the trace stopped at
RekAI's edge — a distributed trace couldn't follow the request into the
provider that actually served it. This works without threading a `trace_id`
parameter through every function from the route handler down to the HTTP call:
the request middleware sets an ambient trace id in a `contextvars.ContextVar`
(`rekai/tracing.py`), and `rekai/providers/base.py`'s `trace_headers()` reads it
at the point of the outbound call. A `ContextVar` rather than a plain module
global so concurrent requests can't leak into each other's trace; outside a
request (e.g. a provider invoked directly in a unit test) it's unset, and no
`traceparent` header is sent at all rather than a synthetic one.
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

Per-provider request counts are their own family, `rekai_provider_requests_total
{provider="…"}`, **not** `rekai_requests_total{provider="…"}`. Emitting a bare
series and a labelled one under one metric name makes `sum(rekai_requests_total)`
count every request twice, and Prometheus treats inconsistent label sets within a
family as an error.

### Errors

`rekai_errors_total` is a single number covering everything from a bad Bearer
token to an upstream outage — useful for an alert threshold, useless for
deciding what to do. Two breakdowns sit beside it:

- `rekai_errors_by_kind_total{kind="…"}` — `unauthorized`, `rate_limited`,
  `concurrency_limit`, `budget_exceeded`, `payload_too_large`,
  `guardrail_blocked`, `idempotency_error`, `provider_error`. This separates
  "callers are misbehaving" from "we are down".
- `rekai_provider_errors_total{provider="…",status="…"}` — recorded for **every**
  upstream failure, including non-transient 4xx and the final attempt in a
  fallback chain. Against `rekai_provider_requests_total` that makes a
  per-provider success rate computable, which is the number that decides whether
  a fallback chain is worth having.

Both label sets are drawn from fixed sets (RekAI's own error codes; registered
provider names × HTTP statuses), so neither grows with traffic. Neither is part
of the persisted snapshot — like the histograms, they are `/metrics`-only, and
`seed()` clears them so a breakdown can't outlive the `errors_total` it explains.

### Latency

Three histograms, all on the OpenTelemetry GenAI advisory bucket boundaries for
`gen_ai.client.operation.duration` (a doubling ladder from 10 ms to ~82 s — the
right shape for LLM calls, unlike the default 5 ms–10 s HTTP ladder), so they
line up with any other GenAI-instrumented hop:

| Metric | Labels | Answers |
|---|---|---|
| `rekai_request_duration_seconds` | `path` | how long the whole hop took |
| `rekai_provider_duration_seconds` | `provider`, `operation` | how long the upstream took (retries included — the caller waited for those too) |
| `rekai_stream_ttft_seconds` | `provider` | time to first streamed token |

The point of having the first two is the **gap between them**: that difference is
RekAI's own overhead, and without it "the gateway is slow" and "the upstream is
slow" are the same observation. TTFT is separate because on a stream, total
duration mostly measures how long the answer is, not how responsive it was.

Two honest caveats. `path` is the matched **route template**, so
`/admin/keys/{key}` is one series rather than one per key; an unmatched request
(404) is bucketed as `<unmatched>`. And on the streaming routes
`rekai_request_duration_seconds` measures time until the response *starts*
streaming, since the handler returns before the body is consumed —
`rekai_stream_ttft_seconds` and `rekai_provider_duration_seconds
{operation="stream"}` are the meaningful ones there.

Histograms are deliberately **not** persisted or merged across replicas the way
the counters are: Prometheus scrapes each replica separately and handles counter
resets itself, so there is nothing to buy for the machinery it would cost.

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
billing. `/v1/models` also reports each model's `pricing`
(`input_per_1m`/`output_per_1m`, or `null` when unknown), so clients can build
cost UIs without hardcoding rates.

There are two ways to override or extend the table:

- `pricing.register_price()` mutates the built-in table directly — process-wide,
  for a plugin or code that runs once at import time.
- `REKAI_PRICING_OVERRIDES` (e.g. `"gpt-4o:2.00:8.00,my-model:0.50:1.50"`) is a
  config-driven override scoped to one `Settings` instance, without touching
  shared global state. This is the one to reach for as an operator: it fixes a
  stale or wrong price, or prices a custom/self-hosted model, **without a code
  change or redeploy** — every cost estimate, budget cap (`REKAI_CLIENT_BUDGET_USD`),
  and the `/v1/models` `pricing` field all read from it. An entry for an
  existing prefix replaces it; a new prefix prices an otherwise-unknown model
  (though it still needs to be a model the provider itself accepts — this only
  affects pricing, not model discovery/routing).

## Guardrails

With `REKAI_GUARDRAILS_ENABLED=true`, RekAI scans the **user** messages of a
chat / chat-stream request for common prompt-injection / jailbreak phrasings
("ignore previous instructions", "reveal your system prompt", "developer mode
enabled", …) before calling a provider. `REKAI_GUARDRAILS_ACTION=flag` (default)
lets the request through with an `X-Guardrail-Flag: <pattern>` header so the
caller can decide; `block` rejects it with `403 guardrail_blocked`. This is a
**heuristic first layer** (OWASP LLM01), not a security boundary — obfuscated or
encoded attacks evade regexes (arXiv:2504.11168), so keep tools least-privileged
and add a classifier-based guardrail where assurance matters. Off by default.

**Why `flag` and not `block` by default.** A regex that is wrong in the blocking
direction deletes a legitimate request with no recourse; wrong in the flagging
direction it costs a header. Since pattern matching cannot be a boundary against
an adversary who can simply rephrase, its realistic value is *signal* — and
signal does not require blocking. Turn on `block` once you have measured the
false-positive rate against your own traffic.

Every pattern requires an object referring to **the model's own instructions or
safety configuration**, which is the design constraint that keeps the
false-positive rate usable. Earlier versions matched a verb plus a bare noun and
flagged ordinary prose — "show me the instructions for assembling this
bookshelf", "override the system clock in a unit test", "summarize this paper
about jailbreak techniques" — each a hard 403 under the old default. The two
corpora in `tests/test_guardrails.py` (21 attack phrasings, 17 benign ones that
all used to be flagged) are the regression gate for any pattern change.

### Output redaction

With `REKAI_OUTPUT_REDACTION_ENABLED=true`, RekAI scans the assistant's
**response** on non-streamed `POST /v1/chat` for common secret/API-key formats
(OpenAI/Anthropic/Stripe/GitHub/Slack keys, AWS access key ids, `Bearer` tokens,
PEM private-key blocks) — the case where a provider echoes back something
sensitive it was handed in a tool result or RAG context (OWASP LLM02). Matches
are replaced with `[REDACTED:<pattern>]` and the response carries an
`X-Redacted: <pattern1,pattern2,…>` header plus a `redacted: ["<pattern>", …]`
field in the body. The scrub runs in `service.handle_chat` — **before** the
response is written to the response cache, the semantic cache, or the
`Idempotency-Key` store — so the raw secret is never persisted (which matters
most with Redis, where it would otherwise sit in plaintext for the whole TTL),
and every later hit or replay serves already-scrubbed text and still reports
what was caught. The edge re-scans as a backstop, which only does anything for
an entry cached *before* redaction was switched on. As with the input guardrail,
this is a **heuristic**, not a
security boundary. It is **not applied to `/v1/chat/stream`**: redacting a
pattern that may span multiple already-sent SSE chunks isn't possible without
buffering the whole reply first, which would defeat streaming — a known,
documented gap rather than a silently-incomplete implementation. Off by default.

## Gateway authentication

Two distinct keys are in play. The **gateway** key authenticates the *client to
RekAI*: set `REKAI_API_KEYS` (comma-separated) and `/v1/*` then requires
`Authorization: Bearer <key>`, compared in constant time; missing/invalid →
`401` with `WWW-Authenticate: Bearer`. With no keys configured the gateway is
open (the default). System endpoints (`/health`, `/metrics`, `/`, `/docs`) stay
open for liveness probes and scraping — except `/metrics`, which can
optionally be locked behind the same Bearer key too (see below), since it
carries a per-client cost breakdown that scraping doesn't need to be public.
This is separate from **BYOK** below, which is the *upstream provider* key.

Rate limiting is **per tenant**: when authenticated, the bucket is keyed by the
API key (a non-reversible `key:<hash>` id, also attached to the structured
access log as `client`) rather than the client IP, so one tenant's traffic can't
exhaust another's budget. Without auth it falls back to the client IP.

It is also **shared across workers/nodes when `REKAI_REDIS_URL` is set**: the
limiter switches from the in-process token bucket to a fixed-window counter
using Redis `INCR` (atomic — a plain get/set cache can't count race-free), so
the limit means what it says instead of silently multiplying by the worker
count. Same limit and same `X-RateLimit-*`/`Retry-After` headers either way;
the only semantic difference is that the shared window resets at its edge
rather than refilling continuously. If Redis errors at runtime the limiter
**fails open** (allows the request, logs a warning) — an outage degrades to
"no rate limiting", not "no service".

### Concurrency cap

`REKAI_MAX_CONCURRENT_REQUESTS` (opt-in, `0` = unlimited) bounds a different
quantity from the rate limiter: **how many `/v1/*` requests are running**, not
how many arrived. For an LLM gateway those diverge badly — 60 requests/minute is
satisfiable by 60 concurrent 60-second streams — and `httpx`'s read timeout
resets on every chunk, so a slow-trickling upstream can hold a streaming request
open indefinitely without ever reaching `REKAI_REQUEST_TIMEOUT_SECONDS`. Nothing
else in the stack bounded occupancy.

Excess requests are **rejected** (429, `Retry-After`, `error:
"concurrency_limit"`), not queued: queueing an LLM request behind a minutes-long
one turns a fast failure the client can act on into a timeout it can't.

Like the body guard it is pure-ASGI and wraps the whole app, which is what makes
it correct for streaming — a `BaseHTTPMiddleware` dispatch returns from
`call_next` when the response *starts*, so a slot released there would be free
before a single token had been sent. It sits *inside* the body guard, so
rejecting an oversized body doesn't consume a slot, and only covers `/v1/*`:
`/health` and `/metrics` stay answerable exactly when the gateway is saturated
and an operator most needs them. Process-local, like the default rate limiter —
with N uvicorn workers the effective cap is N × the configured value.

### Per-client usage

`/v1/usage` and `/metrics` also break down requests, tokens, and cost **per
client** (`usage_by_client` / `rekai_client_*_total{client="…"}`), keyed by the
same masked id used for rate limiting. This covers `/v1/chat`, `/v1/chat/stream`,
and `/v1/embeddings` — including responses served from the content cache or
replayed via `Idempotency-Key` (per-client accounting reflects what the client
received, whereas the global `tokens_total`/`cost_usd_total` counters reflect
RekAI's own upstream spend and don't double-count a cache hit). This is the
per-tenant spend visibility a multi-key deployment needs, without ever
persisting or logging a raw key.

**Who may see whose numbers.** The per-client breakdown names a tenant and what
it spent, so it is scoped by who is asking:

| Endpoint | No gateway auth | Gateway auth on |
|---|---|---|
| `GET /v1/usage` | full `usage_by_client` | **the caller's own row only** |
| `GET /metrics` | full `rekai_client_*` series | `rekai_client_*` only for an authenticated scrape |
| `GET /admin/usage` | not registered unless `REKAI_ADMIN_KEY` is set | full `usage_by_client`, admin key only |

`/v1/usage` sits under `/v1/*`, so `REKAI_API_KEYS` already gates *access* to it
— but every tenant key passed that gate and read every other tenant's spend.
It is now a **tenant view**: aggregate counters (requests, tokens, cost, cache
hits, per-provider) stay fleet-wide, and `usage_by_client` contains just the
calling key's row. `/admin/usage` is the operator's cross-tenant view, gated by
`REKAI_ADMIN_KEY` outside `/v1/*` exactly like `/admin/keys` (rate-limited and
written to the admin audit log the same way). A tenant key is not an admin key.

`/metrics` stays open by default so Prometheus can scrape without a token — the
scalar and per-provider series are operational — but the `rekai_client_*` series
are emitted only to an authenticated caller once gateway auth is in use. An
operator who wants the *whole* endpoint behind the key still sets
`REKAI_METRICS_REQUIRE_AUTH=true` (a no-op if no keys are configured — open
either way, same fallback as `/v1/*`). With no gateway auth configured there are
no tenants to separate and nothing is withheld anywhere.

### Per-client budget cap

`REKAI_CLIENT_BUDGET_USD` (opt-in, unset by default) turns that same
`usage_by_client` cost figure into an enforceable spend cap: once a client's
cumulative cost reaches the configured USD amount, further `/v1/*` requests
from that client get `402 Payment Required` (`X-Budget-Remaining: 0`) — checked
in the same middleware pass as auth and rate limiting, before any provider call
is made, so an over-budget client can't rack up more spend. By default the cap
is lifetime-until-reset (matching `usage_by_client`'s own lifetime counters);
an operator lifts it by resetting metrics or raising the limit, or see
`REKAI_CLIENT_BUDGET_WINDOW_SECONDS` below for a cap that resets on its own.
Distinct from rate limiting: rate limiting bounds *request rate*, this bounds
*cumulative cost*.

`REKAI_CLIENT_BUDGETS_USD` overrides the global cap per API key (e.g.
`"sk-a:5.00,sk-b:20.00"`) — a key not listed falls back to
`REKAI_CLIENT_BUDGET_USD`. There's no per-IP override (only per-key, since an
IP isn't a stable tenant identity); a deployment that needs different caps per
tenant should have gateway auth enabled.

`REKAI_CLIENT_BUDGET_WINDOW_SECONDS` (opt-in, unset by default) time-boxes the
cap instead of leaving it lifetime-cumulative: once set, the spend compared
against the cap is only what a client has spent in the *current* fixed window
(e.g. `86400` = daily, `2592000` = 30 days), using the same epoch-aligned
`int(now / window)` bucketing `RedisRateLimiter` uses for rate-limit windows —
not a rolling window counted from a client's first request. This is tracked in
a structure separate from `usage_by_client` (which stays lifetime for
`/v1/usage` and `/metrics` observability, so a window rollover never erases
those historical totals), and — unlike `usage_by_client` — is **not**
persisted across a restart: the current window's spend starts back at $0 after
a restart. That's the same "approximate, not exact billing" tradeoff already
applied elsewhere in RekAI (see cost estimation above), and budget enforcement
was already process-local/best-effort across workers even without this
feature, so nothing that worked before regresses. When a window is configured,
a 402 also carries an `X-Budget-Reset` header (the unix timestamp of the next
window boundary), so a client knows exactly when it can retry.

Both per-client structures are **bounded**: `REKAI_MAX_TRACKED_CLIENTS`
(default 10,000; `0` = unlimited) caps how many distinct client ids are kept in
`usage_by_client` and the budget-window store. Without gateway auth the client
id is the raw request IP, so an internet-facing deployment would otherwise
accumulate one entry per IP forever — persisted across restarts via the metrics
snapshot, no less. At the cap, admitting a new client evicts the tracked client
with the fewest requests (the budget-window store first clears entries from
already-expired windows, which are dead weight). Eviction resets that client's
lifetime-budget baseline — the same "approximate, not billing" tradeoff as
above; a deployment that needs exact caps for a known tenant set should size
the cap above its tenant count (or set `0` behind auth, where the key space is
operator-controlled). `seed()` applies the same cap when loading a persisted
snapshot, keeping the busiest clients.

### Web app support

The web app (`apps/web`) stores two distinct keys in `localStorage`, both only
in the browser and never persisted server-side: the BYOK provider key
(`rekai.providerKey`, sent as `X-Provider-Key`) under Settings, and, separately,
a gateway key (`rekai.gatewayKey`, sent as `Authorization: Bearer`) for
deployments with `REKAI_API_KEYS` configured. Every `/v1/*` call from the app
(`chat`, `chat/stream`, `embeddings`, `models`, `usage`) attaches the gateway
key when one is set; without it, enabling gateway auth would 401 every page.

### Dynamic key management

`REKAI_API_KEYS` is static — changing it needs a redeploy. `REKAI_DYNAMIC_KEYS_ENABLED`
adds a second, runtime-managed set of keys an operator can add or revoke
through an admin API instead, e.g. to onboard a new tenant or cut off one
that's misbehaving without restarting the process:

- `GET /admin/keys` — list static and dynamic keys, masked (`sk-a…b123`).
- `POST /admin/keys {"key": "..."}` — add a key (`201`).
- `DELETE /admin/keys/{key}` — revoke a key (`200`, or `404` if unknown).

The web app's `/admin` page wraps all three in a form instead of curl-only
access — its own admin-key field (`rekai.adminKey` in `localStorage`, a third
credential distinct from the provider and gateway keys above), a masked
static/dynamic key list, and add/revoke forms. Since only a key's masked form
is ever returned, revoking one from the page still needs the raw key typed
back in — the UI can't offer a "click to revoke" from the masked list, because
the backend genuinely never has the raw value to offer back. If
`REKAI_ADMIN_KEY` isn't configured, the page shows a notice instead of the
forms (a `404`, since the routes aren't registered — see below — not a `401`).

All three require `Authorization: Bearer <REKAI_ADMIN_KEY>` — a credential
distinct from any tenant key. **The admin API is only registered at all when
`REKAI_ADMIN_KEY` is set** (unset = the routes don't exist, not just
unauthenticated), and it lives outside `/v1/*` so it isn't subject to the
tenant gateway-auth gate above. Requests using a dynamically-added key work
exactly like a static one everywhere else (rate limiting, per-client usage,
budget caps). Dynamic keys are stored via the same cache backend as the
response cache (Redis when `REKAI_REDIS_URL` is set — shared across
workers/nodes — else the process-local `MemoryCache`, same caveat as the rate
limiter and idempotency store without Redis).

`/admin/*` also has its own rate limit (`REKAI_ADMIN_RATE_LIMIT_*`, on by
default whenever `REKAI_ADMIN_KEY` is set — 20 requests/60s), built the same
way as the tenant limiter (Redis-shared when configured, else process-local,
fails open on a Redis error) but keyed by client IP and sized separately, since
admin traffic and tenant traffic shouldn't share one budget. The check order is
the opposite of the tenant gateway-auth gate above, deliberately: the tenant
gate checks auth *before* consuming rate-limit budget (so a guesser can't burn
a real tenant's allowance), but the admin gate checks rate limit *before* the
key check, so a wrong guess still counts. There's one shared secret to defend
here, not many tenant-specific ones, so the threat is brute-forcing it — a
guess that doesn't consume budget wouldn't be throttled at all. A firewall/VPN
in front of `/admin/*` is still the primary control; this is a backstop for
when that's not in place (or is bypassed from inside the network).

Every admin request — successful, unauthorized, rate-limited, or a revoke of
an unknown key — is written to a dedicated audit log (`rekai.admin`, distinct
from the general `rekai.access` log), with the masked key, the action
(`add_key`/`revoke_key`/`revoke_key_not_found`/`list_keys`/`auth_failed`/
`rate_limited`), and the caller's IP. There's no per-admin identity beyond the
shared `REKAI_ADMIN_KEY` secret, so IP is the best attribution available — but
every mutation is traceable after the fact,
including failed-auth probes against the endpoint. The raw key is never
logged, only its masked form.

Unlike BYOK below (transient, never stored), dynamic keys *are* persisted
server-side, so `REKAI_DYNAMIC_KEYS_ENCRYPTION_KEY` (a Fernet key from
`rekai.security.generate_key()`) encrypts the blob at rest using the same
`KeyCipher` helper BYOK-vault deployments already have available — useful if
`REKAI_REDIS_URL` points at shared infra the operator doesn't fully trust.
Unset (default) stores plaintext, unchanged from before this existed. A blob
that fails to decrypt (wrong/rotated key, or a plaintext blob predating
encryption being turned on) is treated as empty rather than crashing every
request that checks auth — an operator sees the keys "disappear" and knows to
re-add them, rather than the gateway falling over.

## BYOK

Provider keys arrive per request via the `X-Provider-Key` header. They are
passed straight to the provider call and never logged, cached, or persisted. A
server-side default key (e.g. `REKAI_OPENAI_API_KEY`) is used only when no BYOK
header is present.

### Readiness

`/health` reports `provider_status` per provider: `ready` (usable now — keyless,
or a server-side key is configured) or `byok_only` (needs an `X-Provider-Key`).
Key-requiring providers override `server_key_configured()` to check their key.

`status` is `ok` or **`degraded`** — the latter when at least one provider is
parked in cooldown after a 429 or repeated 5xx, with `parked_providers` giving
each one's remaining seconds. Previously the field was typed `Literal["ok"]` and
could not be anything else, even though the cooldown and circuit-breaker
machinery already knew exactly which providers were unavailable; the only
external evidence was the `rekai_cooldowns_total` counter, which says something
happened but not what is happening now.

Two deliberate limits:

- **Degraded is still HTTP 200.** The gateway is serving, just not from every
  backend. Failing an orchestrator's liveness probe because one of five
  providers is parked would take down a working deployment.
- **`/health` does no I/O** — no upstream probe, no Redis ping. An
  unauthenticated endpoint that makes the gateway call every provider on demand
  is a request amplifier, and a health check that can block on a slow dependency
  is worse than one that can't. The consequence is that `parked_providers` is
  this worker's local view: a cooldown another replica recorded in Redis isn't
  reflected.

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
