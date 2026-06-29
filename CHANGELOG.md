# Changelog

All notable changes to RekAI are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Tool / function calling** — `ChatRequest` accepts OpenAI-style `tools` and
  `tool_choice` (passed through); the model's `tool_calls` are returned on
  `ChatResponse`. Messages support `tool_calls`/`tool_call_id`/`name` and
  optional `content` for full round-trips. (Non-streaming; OpenAI-compatible.)
  Both SDKs expose `tools`/`tool_choice` and surface `tool_calls`.
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

[Unreleased]: https://github.com/shizukutanaka/RekAI/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/shizukutanaka/RekAI/releases/tag/v1.0.0
