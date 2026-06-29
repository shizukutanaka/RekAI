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
- [ ] Demo deployment
- [ ] GitHub Release + release notes
- [ ] Examples (`docs/`, client snippets)
- [ ] v1.0 tag

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

- **v1.x** — stabilize the core, more providers, advanced streaming.
- **v2.x** — plugin ecosystem for providers and policies.
- **v3.x** — advanced multi-provider orchestration (fallback, load-balancing).
- **v4.x** — agent workflows and automation.
