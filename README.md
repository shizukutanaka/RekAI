# RekAI

> A lightweight, self-hostable **AI router & gateway** with provider abstraction, response caching, BYOK (Bring Your Own Key), and a built-in chat UI.

RekAI sits between your application and multiple LLM providers (OpenAI, Ollama, …). It routes each request to the right provider, caches responses to cut cost and latency, and lets every user bring their own API key.

[![CI](https://github.com/shizukutanaka/RekAI/actions/workflows/ci.yml/badge.svg)](https://github.com/shizukutanaka/RekAI/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

---

## ✨ Features

- **Provider abstraction** — one API, many backends (`openai`, `anthropic`, `ollama`, plus an `echo` provider for local dev/tests).
- **Smart routing** — pick a provider explicitly, or let RekAI choose by model name / configured default.
- **Streaming** — Server-Sent Events at `/v1/chat/stream` for token-by-token responses.
- **Response cache** — Redis-backed with an automatic in-memory fallback. Identical requests are served instantly and for free.
- **Cost awareness** — each response carries an estimated USD cost; cumulative spend is exposed at `/v1/usage`.
- **BYOK** — users supply their own provider key per request (`X-Provider-Key`); keys are never persisted.
- **Rate limiting** — simple per-client token bucket out of the box.
- **OpenAPI** — auto-generated docs at `/docs` and a machine-readable schema at `/openapi.json`.
- **Observability** — structured logging and a Prometheus-style `/metrics` endpoint.
- **Chat UI** — a Next.js front-end to try it all in the browser.

## 🏗️ Architecture

```
                ┌──────────────┐
   Browser ───▶ │  Next.js Web │ ─┐
                └──────────────┘  │  HTTP (X-Provider-Key)
                                  ▼
                         ┌─────────────────┐
                         │   FastAPI API   │
                         │  ┌───────────┐  │
                         │  │  Router   │  │ ──▶ chooses provider
                         │  ├───────────┤  │
                         │  │  Cache    │  │ ──▶ Redis / memory
                         │  ├───────────┤  │
                         │  │ Providers │  │ ──▶ OpenAI · Ollama · Echo
                         │  └───────────┘  │
                         └─────────────────┘
```

See [`docs/architecture.md`](./docs/architecture.md) for details.

## 🚀 Quick start

### With Docker (recommended)

```bash
git clone https://github.com/shizukutanaka/RekAI.git
cd RekAI
cp apps/api/.env.example apps/api/.env   # optional: add provider keys
docker compose up --build
```

- API:  http://localhost:8000  (docs at http://localhost:8000/docs)
- Web:  http://localhost:3000

### Local development (without Docker)

**Backend**

```bash
cd apps/api
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn rekai.main:app --reload
```

**Frontend**

```bash
cd apps/web
npm install
npm run dev
```

## 🧪 Try it

The default `echo` provider needs no API key, so the stack works out of the box:

```bash
curl -s http://localhost:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"model":"echo","messages":[{"role":"user","content":"hello"}]}' | jq
```

Use a real provider by passing your own key (BYOK):

```bash
curl -s http://localhost:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -H 'X-Provider-Key: sk-...' \
  -d '{"provider":"openai","model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

Runnable client snippets (curl, Python, JavaScript) live in
[`examples/`](./examples).

## 📦 Monorepo layout

```
RekAI/
├── apps/
│   ├── api/   # FastAPI backend (router, cache, providers, BYOK)
│   └── web/   # Next.js chat UI
├── docs/      # architecture & guides
├── docker-compose.yml
└── .github/   # CI, issue & PR templates
```

## 🗺️ Roadmap

See [`docs/roadmap.md`](./docs/roadmap.md). In short:

| Milestone | Scope |
|-----------|-------|
| **M1 Foundation** | Repo, Docker, FastAPI, Next.js, CI, README |
| **M2 Core** | Router, Cache, Providers, BYOK |
| **M3 UI** | Chat, settings, usage |
| **M4 Quality** | Tests, OpenAPI, logging, security |
| **M5 Release** | OSS docs, demo, v1.0 |

## 🤝 Contributing

Contributions are welcome! Read [CONTRIBUTING.md](./CONTRIBUTING.md) and our [Code of Conduct](./CODE_OF_CONDUCT.md).

## 📄 License

[MIT](./LICENSE) © RekAI contributors
