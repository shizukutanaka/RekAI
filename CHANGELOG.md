# Changelog

All notable changes to RekAI are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Gateway-key support in `examples/`** — `curl.sh` and all 7 Python/JS
  example scripts only ever sent the BYOK provider key; none could reach a
  gateway with `REKAI_API_KEYS` configured. All now read a `REKAI_GATEWAY_KEY`
  env var and send it as `Authorization: Bearer`, matching the SDK fix above.
- **Documented `REKAI_APP_NAME` and `REKAI_REQUEST_TIMEOUT_SECONDS` in
  `.env.example`** — both are real, active `Settings` fields (the latter
  controls every outbound HTTP timeout to a provider — chat, streaming,
  embeddings) that had no entry in the example file, found via a
  config-fields-vs-`.env.example` diff. No behavior change.
- **Gateway-key support in both SDKs** — the Python and JS clients only ever
  sent the BYOK provider key (`X-Provider-Key`); neither could authenticate to
  a gateway with `REKAI_API_KEYS` configured, since that needs a separate
  `Authorization: Bearer` header. Both now accept a `gateway_key`/`gatewayKey`
  (constructor default, overridable per call) and send it on every `/v1/*`
  call (`chat`, `stream`, `embeddings`, `models`, `usage`).
- **`X-Content-Type-Options: nosniff` on all API responses** — set in the
  existing `_request_context` middleware alongside `X-Request-ID` /
  `X-RekAI-Version`, closing the gap where the web app got security headers
  but the API itself didn't.
- **Favicon** — `apps/web/app/icon.svg`; the app previously shipped no icon at
  all, so browsers requested a nonexistent `/favicon.ico`. Uses Next.js's
  built-in `app/icon.svg` convention (auto-linked in `<head>`, no other
  wiring needed).
- **Custom error and 404 pages** — `apps/web/app/error.tsx` (client-side error
  boundary with a "Try again" reset button) and `apps/web/app/not-found.tsx`
  (styled 404 matching the rest of the app) replace Next.js's generic
  defaults.
- **Web security headers** — `apps/web/next.config.js` now sets
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: strict-origin-when-cross-origin`, and a `Content-Security-Policy`
  (`connect-src` includes `NEXT_PUBLIC_API_URL` so API calls aren't blocked).
  Verified live: all 5 pages load and a real chat round-trip completes with
  zero CSP violations in the browser console.
- **Non-root Docker users** — both Dockerfiles now drop root before running
  the server (`rekai` for the API image, the built-in `node` user for the
  web image); the web image's `deps` stage also switched from `npm install`
  to `npm ci` for reproducible builds. Reviewed for correctness (standard,
  well-documented patterns) but not build-verified in this session — the
  sandbox's Docker daemon isn't available (`dockerd` fails to start here);
  `docker compose config` validates cleanly.
- **Web E2E suite** — `apps/web/e2e/` (Playwright) promotes three flows
  hand-verified ad-hoc throughout development into committed regression
  tests: sending a chat message, gateway auth locking out the app until a
  key is saved in Settings, and the `/admin` runtime key-management UI
  (wrong key errors, add/revoke). Each spec starts/stops its own API process
  with the `REKAI_*` env it needs (fixed ports, so specs run serially — see
  `apps/web/e2e/README.md`). `npm run e2e`.
- **Time-boxed per-client budget windows** — `REKAI_CLIENT_BUDGET_WINDOW_SECONDS`
  turns `REKAI_CLIENT_BUDGET_USD` from a lifetime-until-reset cap into a fixed
  window (e.g. `86400` for daily, `2592000` for 30 days), using the same
  epoch-aligned `int(now / window)` bucketing `RedisRateLimiter` uses. Unset
  (default) = today's lifetime-cumulative behavior, unchanged. The 402
  response gets a new `X-Budget-Reset` header (next window boundary) when a
  window is configured. Tracked separately from `usage_by_client` and not
  persisted across restarts — see docs/architecture.md. Closes the last
  open item from this session's feature-triage list.
- **`/admin/*` rate limiting** — on by default whenever `REKAI_ADMIN_KEY` is
  set (`REKAI_ADMIN_RATE_LIMIT_*`, 20 requests/60s), sized and keyed
  separately from the tenant rate limit (by IP, since there's no per-admin
  identity). Deliberately checked *before* the admin-key check — the opposite
  order from the tenant gateway-auth gate — so a wrong-key guess still
  consumes budget: the threat here is brute-forcing the one shared secret, not
  fairness between tenants. A firewall/VPN in front of `/admin/*` remains the
  primary control; this is a backstop.
- **Web UI for dynamic key management** — `/admin` in the web app wraps the
  API's `/admin/keys` (add/revoke/list runtime keys, `REKAI_DYNAMIC_KEYS_ENABLED`)
  in a page instead of curl-only access, with its own admin-key field
  (local storage, distinct from the gateway/provider keys) and a clear notice
  when `REKAI_ADMIN_KEY` isn't configured server-side.
- **`traceparent` forwarded upstream** — RekAI parsed and returned W3C Trace
  Context, but the trace stopped at its edge: the outbound HTTP call to the
  actual provider (OpenAI/Anthropic/Gemini/Ollama/custom; chat, streaming, and
  embeddings) never carried a `traceparent`, so a distributed trace couldn't
  follow the request into whichever provider served it. Now every provider
  call attaches a `traceparent` continuing the request's trace with a fresh
  span id, via an ambient `ContextVar` set by the request middleware — no
  `trace_id` parameter threading required down through the call stack.
- **Admin API audit log** — every `/admin/keys` request (add, revoke, list,
  and unauthorized attempts) is now written to a dedicated `rekai.admin`
  logger with the action, masked key, and caller IP — the key issuance/
  revocation operations previously left no record beyond the generic access
  log line. The raw key is never logged.
- **`REKAI_PRICING_OVERRIDES`** — override or extend the built-in per-model
  price table without a code change or redeploy, e.g.
  `"gpt-4o:2.00:8.00,my-model:0.50:1.50"`. Config-driven (scoped to one
  `Settings` instance, doesn't mutate shared global state like
  `pricing.register_price()` does) and read by every cost estimate, budget
  cap, and the `pricing` field in `/v1/models` — closing the gap where a
  stale/wrong price, once shipped, needed a code change to fix.
- **Circuit breaker for repeated 5xx** — a 429 already parked a provider
  immediately (an explicit "back off" signal); a bare 5xx did not, so a
  provider stuck returning 500s paid for a full retry-with-backoff cycle on
  *every* request before falling over to a fallback. `REKAI_CIRCUIT_BREAKER_THRESHOLD`
  (default 3) now parks a provider the same way after that many *consecutive*
  5xx failures across separate requests (any success resets the count) —
  covers the fallback-chain path and the streaming path.
- **Redis-shared rate limiting** — with `REKAI_REDIS_URL` set, the per-tenant
  rate limit is enforced with a fixed-window Redis `INCR` counter shared by
  all workers/nodes, instead of each process keeping its own token bucket
  (which silently multiplied the effective limit by the worker count). Same
  headers (`X-RateLimit-*`, `Retry-After`); fails open on a Redis outage so
  rate limiting degrades before availability does. Verified live with two
  uvicorn workers: exactly 5 of 10 requests pass at a limit of 5.
- **Dynamic API key management** (opt-in) — `REKAI_DYNAMIC_KEYS_ENABLED` adds a
  runtime-managed set of keys on top of the static `REKAI_API_KEYS`, so an
  operator can onboard or cut off a tenant without a redeploy: `GET/POST
  /admin/keys` and `DELETE /admin/keys/{key}`, guarded by a separate
  `REKAI_ADMIN_KEY` (the admin routes aren't registered at all unless it's
  set). Dynamically-added keys work everywhere a static one does — rate
  limiting, per-client usage, budget caps. Stored via the existing cache
  backend (Redis if configured, else process-local).
  `REKAI_DYNAMIC_KEYS_ENCRYPTION_KEY` (a Fernet key) encrypts that storage at
  rest, since unlike BYOK these keys are actually persisted server-side; unset
  (default) stores plaintext as before.
- **`REKAI_METRICS_REQUIRE_AUTH`** (opt-in) — `/metrics` is open by default
  (so Prometheus can scrape without a token) even when `REKAI_API_KEYS` gates
  `/v1/*`, but it now carries a per-client cost breakdown. This flag locks it
  behind the same Bearer key for operators who consider that sensitive; a
  no-op when no keys are configured. `/v1/usage` was already gated (it's under
  `/v1/*`) — this closes the one endpoint that wasn't.
- **Per-client budget cap** (opt-in) — `REKAI_CLIENT_BUDGET_USD` turns the
  per-client cost tracked in `usage_by_client` into an enforceable spend cap:
  once a client's cumulative cost reaches it, further `/v1/*` requests from
  that client get `402 Payment Required` (`X-Budget-Remaining: 0`), checked
  before any provider call is made. Unset (default) = no cap.
  `REKAI_CLIENT_BUDGETS_USD` (e.g. `"sk-a:5.00,sk-b:20.00"`) overrides the cap
  per API key, falling back to the global default for unlisted keys — needed
  for a multi-tenant deployment where different clients need different caps.
- **Per-client usage metrics** — `/v1/usage` and `/metrics` now break down
  requests, tokens, and cost per client (`usage_by_client` /
  `rekai_client_*_total{client="…"}`), keyed by the same masked `key:<hash>` id
  used for per-tenant rate limiting (or the client IP with no gateway auth).
  Covers chat, streaming, and embeddings, including cached/idempotent-replay
  responses. The raw key is never stored or logged. `usage_by_client` round-trips
  through the write-behind metrics store like every other counter — seeded on
  startup, accumulated live, flushed on shutdown — and is covered by a
  dedicated persistence test.
- **Output redaction** (opt-in, OWASP LLM02) —
  `REKAI_OUTPUT_REDACTION_ENABLED=true` scrubs common secret/API-key patterns
  (OpenAI/Anthropic/Stripe/GitHub/Slack keys, AWS access key ids, `Bearer`
  tokens, PEM private-key blocks) from the assistant's reply on non-streamed
  `/v1/chat`, before it's cached or stored for idempotency replay. Sets
  `X-Redacted: <pattern,...>` when it fires. A heuristic (not a security
  boundary); intentionally not applied to `/v1/chat/stream` (see
  `docs/architecture.md`).
- **Redis-shared provider cooldown** — when `REKAI_REDIS_URL` is set, a
  provider's 429 cooldown is written through to and read from Redis (in
  addition to the local, zero-latency check), so a rate limit discovered by one
  worker/node is honoured by the others instead of each rediscovering it
  independently. No Redis configured → unchanged, process-local behaviour.
- **Prompt-injection guardrail** (opt-in, OWASP LLM01) —
  `REKAI_GUARDRAILS_ENABLED=true` scans user messages on `/v1/chat[/stream]` for
  common injection/jailbreak phrasings; `block` (default) rejects with `403`,
  `flag` adds an `X-Guardrail-Flag` header. A heuristic first layer (not a
  security boundary) — see arXiv:2504.11168 on evasion. Off by default.
- **Per-tenant rate limiting** — when gateway auth is on, the rate-limit bucket
  is keyed by the API key (a non-reversible `key:<hash>` id) instead of the
  client IP, so one tenant can't exhaust another's budget; the masked id is also
  attached to the structured access log as `client`. Falls back to IP when
  unauthenticated.
- **Gateway authentication** (opt-in) — set `REKAI_API_KEYS` (comma-separated)
  to require `Authorization: Bearer <key>` on `/v1/*`, compared in constant time
  (`401` + `WWW-Authenticate: Bearer` otherwise). Checked before rate limiting so
  unauthenticated traffic can't consume budget; system endpoints (`/health`,
  `/metrics`) stay open. Distinct from BYOK (the upstream provider key). Empty by
  default (open).
- **W3C Trace Context** — RekAI parses an incoming `traceparent`, continues its
  `trace_id` (emitting a new span id) or starts a fresh trace, returns a
  `traceparent` response header, and attaches `trace_id` to the structured
  access log — so it participates in distributed traces in an OpenTelemetry
  system, dependency-free.
- **Resilience metrics** — `rekai_retries_total` (transient failures retried in
  place) and `rekai_cooldowns_total` (providers parked after a 429) are now
  counted in `/metrics` and `/v1/usage`, and shown on the web usage dashboard,
  so operators can see the retry/cooldown machinery working.
- **Semantic cache** (opt-in) — `REKAI_SEMANTIC_CACHE_ENABLED=true` reuses a
  response when a prior prompt's embedding is within
  `REKAI_SEMANTIC_CACHE_THRESHOLD` cosine similarity (default 0.85), catching
  paraphrases that the exact-match cache misses — the *GPT Semantic Cache*
  approach (arXiv:2411.05276). Entries are bucketed by provider/model/params and
  held in a bounded process-local store. Costs one embedding call per request,
  so use a real embeddings model.
- **Idempotency-Key** — clients can send an `Idempotency-Key` header on
  `POST /v1/chat` / `/v1/embeddings`; a repeat with the same key returns the
  stored first response (`Idempotent-Replay: true`) instead of processing again,
  so a network blip or automatic retry can't double-process. Keyed by the
  client id (not the body), works with `"cache": false`, TTL
  `REKAI_IDEMPOTENCY_TTL_SECONDS` (default 24h).
- **Automatic retry with backoff + jitter** — transient upstream failures
  (5xx / network timeouts) are now retried in place before falling over, with
  exponential backoff and full jitter (`REKAI_RETRY_MAX_ATTEMPTS`, default 2;
  `REKAI_RETRY_BASE_DELAY_SECONDS`, `REKAI_RETRY_MAX_DELAY_SECONDS`). 4xx errors
  are never retried. Applies to chat and embeddings. (A resilience pattern
  widely recommended for LLM API clients — retry transient errors with jittered
  exponential backoff rather than failing or hammering a recovering upstream.)
- **Upstream rate-limit (429) handling** — a provider 429 is now retried
  honouring its `Retry-After` (waiting that long when it's within `max_delay`),
  triggers failover to the next target, and — when ultimately surfaced — its
  `Retry-After` is **passed through to the client** so the caller's SDK backs
  off by the amount the provider asked for (previously 429 was terminal and the
  header was dropped). `ProviderError` gained `retry_after`.
- **Provider cooldown** — after a 429 a provider is parked for its `Retry-After`
  (or `REKAI_PROVIDER_COOLDOWN_SECONDS`, default 30s) and routing skips it in
  favour of a healthy fallback while it cools down, so RekAI stops hammering a
  rate-limited provider across requests. Toggle with
  `REKAI_PROVIDER_COOLDOWN_ENABLED`.

### Fixed
- **Password fields wrapped in `<form>`** — the Settings and Admin pages'
  password/credential inputs (provider key, gateway key, admin key, add/revoke
  runtime key) previously used bare `onClick` handlers, which browsers warn
  about ("password field is not contained in a form") and which break
  Enter-to-submit and password-manager integration. Each is now inside its own
  `<form onSubmit>`.
- **Fail-fast config validation** — enum-like settings (`REKAI_LOG_FORMAT`,
  `REKAI_GUARDRAILS_ACTION`) are now `Literal`-typed and numeric ones carry
  bounds (`SEMANTIC_CACHE_THRESHOLD` in [0,1], `RETRY_MAX_ATTEMPTS`/rate limits
  ≥ 1, TTL/body-size ≥ 0), so a typo or out-of-range value raises a clear error
  at startup instead of silently falling back to wrong behaviour.
- **Streaming 429 now records a provider cooldown** — a rate limit seen on the
  streaming path is now parked like on the non-streaming path, so subsequent
  requests route around the rate-limited provider (previously only non-streaming
  429s did this).
- **Streaming crash on tools conversations** — when a provider didn't report
  usage, the streaming endpoint estimated tokens over the request messages and
  crashed mid-stream on a message with `content: null` (valid in a tool
  round-trip). It now treats null content as empty, so the stream completes with
  an estimated usage summary.
- **Cache correctness with tools** — the chat cache key now includes `tools`
  and `tool_choice`. Previously two requests with identical messages but
  different tools collided, so a tool-less reply could be served for a tools
  request (and vice versa).
- **Rate-limiter memory bound** — the per-client bucket map now prunes idle
  (fully-refilled) buckets once it passes a soft cap (`max_buckets`, default
  10k), so a flood of distinct client keys can't grow memory without bound.
- **Memory cache bound** — `MemoryCache` drops expired entries before growing
  past `max_entries` (default 10k), instead of only evicting on read.
- **Web app couldn't use gateway auth at all** — enabling `REKAI_API_KEYS`
  broke the entire web app (chat, embeddings, and the usage dashboard all got
  silent or opaque `401`s), because it only ever sent the upstream BYOK
  provider key (`X-Provider-Key`), never a gateway `Authorization: Bearer`
  token. Settings now has a separate "Gateway API key" field, stored and sent
  alongside the provider key on every `/v1/*` call; the usage page also
  surfaces a clear "set the gateway key" hint on `401`.

### Changed
- Refreshed the README and architecture docs to reflect the 1.1.0 feature set
  (all five providers, tool calling, embeddings, model discovery + pricing,
  rate-limit headers, JSON logging, SDKs).

## [1.1.0] - 2026-06-29

A backward-compatible feature release building on 1.0.0: text embeddings across
all providers, richer model discovery (types, pricing, filtering), rate-limit
observability, structured logging, and hardening.

### Added
- **Per-model pricing in `/v1/models`** — each entry now carries an optional
  `pricing` (`input_per_1m`/`output_per_1m` USD, or `null` when unknown) from
  the pricing table, so clients can show cost estimates without hardcoding
  rates. The web app and JS SDK `ModelInfo` types include it.
- **Request body size limit** — `/v1/*` requests whose `Content-Length` exceeds
  `REKAI_MAX_BODY_BYTES` (default 1 MB; 0 disables) are rejected with
  `413 Payload Too Large` before parsing, protecting the server from oversized
  payloads. The `413` (and existing `429`) responses are now documented in the
  OpenAPI schema for the chat/embeddings/stream endpoints.
- **`X-RekAI-Version` header** — every response advertises the gateway version
  that served it (exposed via CORS), so clients and proxies can see which
  version answered.
- **Root banner endpoint** — `GET /` returns a small JSON service banner
  (name, version, description, links to `/docs` and `/health`) so hitting the
  bare API URL is friendly instead of a 404.
- **Structured JSON logging** — set `REKAI_LOG_FORMAT=json` to emit one JSON
  object per log line (`ts`, `level`, `logger`, `message`, plus any `extra=`
  fields). The access log now carries structured `method`/`path`/`status`/
  `duration_ms`/`request_id` fields, so logs are machine-parseable in
  production. Defaults to the human-readable text format.
- **`/v1/models?type=` filter** — fetch only `chat` or only `embedding` models
  server-side (invalid values are rejected with 422). The web Embeddings page
  uses it directly instead of filtering client-side.
- **Rate-limit budget hint in the web chat** — after a request the composer
  shows a subtle "N / M requests left in the rate-limit window", read from the
  `X-RateLimit-*` headers via a new `parseRateLimit()` and an `onRateLimit`
  callback on the chat fetch helpers.
- **Graceful rate-limit UX in the web chat** — a 429 now shows a clear
  "Rate limited — retry in Ns." message (from `Retry-After`) instead of a
  generic failure. Required two fixes so the browser can actually read the
  response: CORS is now the outermost middleware (so a short-circuit 429 still
  carries CORS headers) and the custom headers (`Retry-After`, `X-RateLimit-*`,
  `X-Request-ID`, `X-Response-Time-Ms`) are exposed via
  `Access-Control-Expose-Headers`. CORS preflight (`OPTIONS`) no longer consumes
  rate-limit budget. The web fetch helpers share one `errorFromResponse()`.
- **Rate-limit headers** — every `/v1/*` response now carries
  `X-RateLimit-Limit` and `X-RateLimit-Remaining`, and rate-limited responses
  add a standard `Retry-After` (whole seconds until a token frees up, also
  echoed in the detail). `RateLimiter` gained non-consuming `remaining()` and
  `retry_after()` peeks.
- **Container healthchecks & readiness gating** — the web image gained a
  `HEALTHCHECK` (the API already had one), and Docker Compose now starts `web`
  only once `api` is `service_healthy` (which itself waits on Redis). A
  `docker compose up` comes up in dependency order and reports real readiness.
- **Embeddings** — `POST /v1/embeddings` with provider routing, caching, BYOK,
  and metrics. Echo returns deterministic vectors (no key); OpenAI(-compatible)
  calls the real `/embeddings` API. `Provider.embed()` is the extension point.
  Both SDKs expose `embeddings()` (Python `EmbeddingsResult`, JS returns the
  parsed object) for client parity with the chat path. **Ollama** embeddings
  are native via `/api/embed` (keyless, e.g. `nomic-embed-text`) and **Gemini**
  via `:batchEmbedContents` (e.g. `text-embedding-004`) — vectors now span all
  cloud providers like chat. Embeddings responses carry `cost_usd` (input-only
  pricing for `text-embedding-3-*`/`ada-002`; both SDKs surface it). A web
  **Embeddings** playground (`/embeddings`) embeds one-input-per-line and shows
  vector dims, cost, and pairwise cosine similarity. `/v1/models` now tags each
  entry with a `type` (`chat`/`embedding`) and advertises embedding models
  (`list_embedding_models()`), so the playground offers a real model dropdown
  routed to the right provider and the chat selector stays chat-only.
  OpenAI-compatible backends can advertise their own embedding models via
  `REKAI_CUSTOM_EMBEDDING_MODELS`. Runnable
  `examples/{python,javascript}/embeddings.{py,mjs}` show a cosine-similarity
  demo, and `examples/python/semantic_search.py` ranks a corpus against a query
  (the core of RAG retrieval).
- **Tool / function calling** — `ChatRequest` accepts OpenAI-style `tools` and
  `tool_choice` (passed through); the model's `tool_calls` are returned on
  `ChatResponse`. Messages support `tool_calls`/`tool_call_id`/`name` and
  optional `content` for full round-trips. (Non-streaming; OpenAI-compatible.)
  Both SDKs expose `tools`/`tool_choice` and surface `tool_calls`. For
  streaming, OpenAI tool-call deltas are accumulated and returned in the final
  summary event. **Anthropic** tools work natively via format translation
  (OpenAI `tools`/`tool_choice`/`tool_calls` ↔ Anthropic
  `input_schema`/`tool_use`/`tool_result`). **Gemini** likewise via
  `functionDeclarations`/`functionCall`/`functionResponse` — uniform tool
  calling across OpenAI, Anthropic, and Gemini through one API, in both
  non-streaming and streaming modes. A `examples/python/tools.py` demonstrates a
  full call → execute → respond round-trip.
- **Exact provider routing from the web** — the chat UI sends the selected
  model's provider (from `/v1/models`), so custom and explicitly-chosen
  providers route correctly instead of falling back to the default.
- **OpenAI-compatible provider** — set `REKAI_CUSTOM_BASE_URL` to front any
  OpenAI-compatible API (Groq, Together, OpenRouter, Mistral, vLLM, LM Studio…);
  reuses the OpenAI implementation incl. accurate streaming usage.
- **Deploy configs** — a Render Blueprint (`deploy/render.yaml`) provisioning
  Redis + API + Web, plus a deploy guide. The web `Dockerfile` accepts
  `NEXT_PUBLIC_API_URL` as a build arg so the API URL is baked correctly.
- **Regenerate** — re-run the last user turn for a fresh assistant reply,
  without duplicating messages.
- **Max tokens control** — the chat Options panel now exposes a `max_tokens`
  cap (forwarded on both the streaming and non-streaming requests).
- **Streaming usage/cost** — `POST /v1/chat/stream` now emits a final
  `{"usage", "cost_usd", "estimated"}` summary event, and streamed requests are
  counted in `/v1/usage` and `/metrics` (previously only non-streamed were). The
  web chat shows token/cost on streamed replies.
- **SDK streaming usage** — the Python (`on_usage`) and JS (`onUsage`) clients
  now surface the final streaming usage/cost summary via an optional callback.
- **Accurate streaming usage** — providers gained `stream_events()`; all five
  (echo, OpenAI via `stream_options`, Anthropic, Gemini, Ollama) report exact
  token counts during streaming (`estimated: false`), with text estimation as
  the fallback.

## [1.0.0] - 2026-06-29

First public release — a self-hostable AI router & gateway. Runs with a single
`docker compose up`, works out of the box via the keyless `echo` provider, and
exposes one OpenAI-style chat API across five backends with caching, BYOK,
streaming, fallback, cost estimation, and a built-in web UI.

### Added
- **Monorepo foundation** — `apps/api` (FastAPI) and `apps/web` (Next.js),
  Docker Compose, devcontainer, issue/PR templates, and OSS docs
  (README, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY).
- **Router** — provider resolution by explicit choice → model-name prefix →
  configured default.
- **Provider abstraction** with a registry and five backends: `echo` (keyless),
  `openai`, `anthropic`, `gemini`, and `ollama`.
- **Response cache** — Redis with an automatic in-memory fallback and a
  per-request opt-out; deterministic cache keys.
- **BYOK** — per-request provider keys via `X-Provider-Key`, never persisted.
- **Streaming** — `POST /v1/chat/stream` (SSE) with native token streaming for
  echo, OpenAI, Anthropic, Gemini, and Ollama; safe single-chunk fallback.
- **Cost estimation** — per-model price table, `cost_usd` on responses, and a
  `/v1/usage` summary; cumulative cost in `/metrics`.
- **Fallback / failover** — ordered `(provider, model)` chain retried on
  upstream (5xx) errors; 4xx client errors are terminal.
- **Rate limiting** — per-client token bucket on `/v1/*`.
- **Observability** — structured logging, per-request `X-Request-ID` +
  `X-Response-Time-Ms` headers with access logging, Prometheus-style
  `/metrics`, `/v1/usage`, and auto-generated OpenAPI at `/docs`.
- **Persistent metrics** — write-behind persistence of the usage counters to
  Redis (when configured) so `/v1/usage` totals survive restarts; in-memory and
  no-op otherwise.
- **Web UI** — chat with model selector, streaming toggle, an **Options** panel
  (system prompt + temperature), a **Stop** button to cancel a stream,
  conversation persistence across reloads, and cache/provider/token/cost
  indicators; a live **usage dashboard** at `/usage`; a settings page for BYOK
  keys.
- **Examples** — runnable curl, Python (incl. streaming), and JavaScript
  clients.
- **Python SDK** — installable `rekai-client` package (`packages/python-sdk`)
  with `RekAIClient` (`chat`, `stream`, `models`, `usage`, `health`), BYOK,
  and fallback support.
- **JavaScript/TypeScript SDK** — zero-dependency `@rekai/client`
  (`packages/js-sdk`) mirroring the Python client, with TypeScript types and an
  async-generator `stream()`.
- **CI** — GitHub Actions for API (ruff, mypy, pytest), web (lint, vitest,
  build), Python/JS SDK tests, a live-API smoke job, and Docker image builds.
- **Web unit tests** — vitest coverage for the pure client helpers
  (`formatCost`, `parseSSEFrame`).
- **Makefile** — common developer tasks (`make help`).
- **Smoke test** — `scripts/smoke.sh` exercises the core endpoints of a running
  instance (health, chat, stream, usage, models, OpenAPI).
- **pre-commit** — config running ruff (lint + format) and file-hygiene hooks.
- **.dockerignore** for both apps so image builds exclude local
  `node_modules`/`.venv`/caches.
- **Provider readiness** in `/health` (`provider_status`): `ready` vs
  `byok_only` per provider, surfaced as badges on the web Settings page and as
  an inline chat hint when the selected model needs a key that isn't set.

### Fixed
- Web `output: standalone` is now gated behind `NEXT_OUTPUT=standalone` (set by
  the Dockerfile) so local `next start` works and the app hydrates correctly.
- SDK CI now runs `ruff format --check` (previously only `ruff check`), and the
  SDK source was reformatted to match.

[Unreleased]: https://github.com/shizukutanaka/RekAI/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/shizukutanaka/RekAI/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/shizukutanaka/RekAI/releases/tag/v1.0.0
