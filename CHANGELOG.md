# Changelog

All notable changes to RekAI are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
- **Cache correctness with tools** — the chat cache key now includes `tools`
  and `tool_choice`. Previously two requests with identical messages but
  different tools collided, so a tool-less reply could be served for a tools
  request (and vice versa).
- **Rate-limiter memory bound** — the per-client bucket map now prunes idle
  (fully-refilled) buckets once it passes a soft cap (`max_buckets`, default
  10k), so a flood of distinct client keys can't grow memory without bound.
- **Memory cache bound** — `MemoryCache` drops expired entries before growing
  past `max_entries` (default 10k), instead of only evicting on read.

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
