# Roadmap

RekAI follows a "ship a working core, then extend" philosophy. The goal of v1.0
is a self-hostable AI gateway that runs with a single `docker compose up`.

## Milestones

### M1 — Foundation ✅
- [x] Monorepo layout (`apps/api`, `apps/web`)
- [x] FastAPI backend skeleton
- [x] Next.js frontend skeleton
- [x] Docker + Docker Compose
- [x] GitHub Actions CI (lint, type, test, build)
- [x] README, CONTRIBUTING, LICENSE, devcontainer

### M2 — Core ✅
- [x] Router with explicit / prefix / default resolution
- [x] Provider abstraction + registry
- [x] OpenAI, Ollama, and Echo providers
- [x] Redis cache with in-memory fallback
- [x] BYOK via `X-Provider-Key`

### M3 — UI ✅
- [x] Chat interface
- [x] Model selector
- [x] Settings page (BYOK key storage)
- [x] Cache / provider / token / cost indicators
- [x] Usage dashboard (/usage)

### M4 — Quality ✅
- [x] Test suite (router, cache, providers, security, endpoints)
- [x] OpenAPI auto-generated at `/openapi.json` and `/docs`
- [x] Structured logging
- [x] Prometheus-style `/metrics`
- [x] Rate limiting

### M5 — Release
- [x] Anthropic (Claude) provider
- [x] Google Gemini provider
- [x] Streaming responses (SSE)
- [x] Cost estimation + `/v1/usage` summary
- [x] Provider fallback / failover
- [x] Client examples (curl, Python, JavaScript)
- [x] Python and JavaScript/TypeScript SDKs
- [x] Release notes (CHANGELOG v1.0.0)
- [x] Version bump to 1.0.0 across packages
- [x] Deploy configs (Render Blueprint + Docker; see `deploy/`)
- [ ] Live demo instance (maintainer action)
- [ ] GitHub Release + `v1.0.0` tag (maintainer action)

### Shipped after v1.0 (v1.1 – v1.2) ✅

Post-1.0 hardening and reach, all released — see [CHANGELOG.md](../CHANGELOG.md)
for the full detail:

- [x] **OpenAI-compatible `POST /v1/chat/completions`** — drop-in for the
  OpenAI SDK / LangChain, non-streaming and streaming, with
  `response_format` (structured outputs) passthrough across the API and both
  SDKs (v1.2)
- [x] **Dynamic API-key management** — add/revoke tenant keys at runtime via
  `/admin/keys` (+ web admin UI), optionally encrypted at rest (v1.1)
- [x] **Per-client budgets** — lifetime and time-boxed
  (`REKAI_CLIENT_BUDGET_WINDOW_SECONDS`) spend caps, bounded tracking
  (`REKAI_MAX_TRACKED_CLIENTS`) (v1.1 – v1.2)
- [x] **Resilience** — retry + circuit breaker + provider cooldown;
  Redis-shared rate limiting across workers/nodes (v1.1)
- [x] **Observability** — W3C `traceparent` propagated to upstream providers,
  OpenTelemetry GenAI semantic-convention attributes on access logs (v1.1 – v1.2)
- [x] **Security hardening** — prompt-injection guardrail + output redaction,
  a real hard cap on request body size (chunked-transfer-encoding safe),
  security headers (v1.1 – v1.2)
- [x] **Gateway-key (Bearer) auth** in both SDKs and all examples;
  connection pooling to upstream providers (v1.2)

## Explicitly out of scope for v1.0

To keep a single maintainer productive, these are deferred to v2.x and only
have interface seams reserved today:

- Kubernetes-first operations
- Multi-cloud deployment
- Enterprise SSO
- Advanced multi-tenancy
- Complex billing
- Dozens of provider integrations

## Beyond v1.0

- **v1.x** — in progress: OpenAI-compatible API, structured outputs, dynamic
  keys, budgets, and observability shipped in v1.1 – v1.2 (above); further
  provider integrations and advanced streaming to follow.
- **v2.x** — plugin ecosystem for providers and policies.
- **v3.x** — advanced multi-provider orchestration (fallback, load-balancing).
- **v4.x** — agent workflows and automation.
