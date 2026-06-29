# Changelog

All notable changes to RekAI are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
- **Observability** — structured logging, Prometheus-style `/metrics`,
  `/v1/usage`, and auto-generated OpenAPI at `/docs`.
- **Web UI** — chat with model selector, streaming toggle, and cache/provider/
  token/cost indicators; a live **usage dashboard** at `/usage`; a settings
  page for BYOK keys.
- **Examples** — runnable curl, Python (incl. streaming), and JavaScript
  clients.
- **CI** — GitHub Actions for API (ruff, mypy, pytest), web (lint, build), and
  Docker image builds.
- **Makefile** — common developer tasks (`make help`).

[Unreleased]: https://github.com/shizukutanaka/RekAI/commits/main
