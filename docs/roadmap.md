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

### Blocked on repository permissions, not on code

Everything below is finished in the tree and cannot be completed by the agent
sessions working on this repo, because the GitHub App token they use can push
neither tags nor anything under `.github/workflows/`. A maintainer with write
access finishes each in one command:

- [ ] **Install CI** — `git mv .github/ci-workflow.yml .github/workflows/ci.yml`
  and push. The workflow is complete and covers every gate in `CLAUDE.md` plus a
  `stack` job that builds both images and smoke-tests the running containers,
  which is the only build verification the images can get (no Docker daemon in
  the agent sandbox). See [`.github/README.md`](../.github/README.md).
- [ ] **Tag the release** — `git tag -a v1.3.0 -m "RekAI 1.3.0" && git push
  origin v1.3.0`. Every version string in the monorepo is already at 1.3.0 and
  `CHANGELOG.md [1.3.0]` is the release notes.
- [ ] **Publish the GitHub Release** from that CHANGELOG section.
- [ ] **Live demo instance** — `deploy/render.yaml` provisions the whole stack.

### Shipped after v1.0 (v1.1 – v1.3) ✅

Post-1.0 hardening and reach, all released — see [CHANGELOG.md](../CHANGELOG.md)
for the full detail:

- [x] **Correctness and safety of what already shipped** (v1.3) — the release
  is almost entirely defect work found by auditing each subsystem against its
  own specification: W3C Trace Context conformance and `tracestate`
  propagation, the provider's real `finish_reason` instead of a synthesised
  `"stop"` (a truncated answer used to be indistinguishable from a complete
  one), secret redaction on the streaming path, a rate limiter that stays
  bounded under a flood of distinct clients, script-aware token estimation, a
  non-ASCII `Authorization` header returning 401 rather than an unhandled 500,
  a total request deadline (`REKAI_REQUEST_DEADLINE_SECONDS`) bounding retries
  *and* the fallback chain rather than only each upstream call, a compose file
  that actually delivers the provider keys the quickstart tells you to set, a
  startup guard that refuses to run as an open proxy for the operator's own
  provider keys, reachability for the keyless OpenAI-compatible backends the
  README advertises (vLLM, LM Studio), and persisted metrics snapshots that
  expire instead of leaking one Redis key per process start

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

- **v1.x** — OpenAI-compatible API, structured outputs, dynamic keys, budgets
  and observability shipped in v1.1 – v1.2; v1.3 is the correctness pass over
  all of it (above). Further provider integrations and advanced streaming to
  follow.
- **v2.x** — plugin ecosystem for providers and policies.
- **v3.x** — advanced multi-provider orchestration (fallback, load-balancing).
- **v4.x** — agent workflows and automation.
