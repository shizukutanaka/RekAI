# RekAI

> A lightweight, self-hostable **AI router & gateway** with provider abstraction, response caching, BYOK (Bring Your Own Key), and a built-in chat UI.

RekAI sits between your application and multiple LLM providers (OpenAI, Anthropic, Gemini, Ollama, and any OpenAI-compatible endpoint). It routes each request to the right provider, caches responses to cut cost and latency, supports chat, streaming, tool calling, and embeddings through one API, and lets every user bring their own API key.

[![CI](https://github.com/shizukutanaka/RekAI/actions/workflows/ci.yml/badge.svg)](https://github.com/shizukutanaka/RekAI/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

---

## ✨ Features

- **Provider abstraction** — one API, many backends (`openai`, `anthropic`, `gemini`, `ollama`, plus an `echo` provider for local dev/tests). Point at **any OpenAI-compatible endpoint** (Groq, Together, OpenRouter, Mistral, vLLM, LM Studio…) with one env var.
- **Smart routing** — pick a provider explicitly, or let RekAI choose by model name / configured default.
- **Fallback / failover** — on an upstream error, retry down a configured chain of `(provider, model)` targets.
- **Streaming** — Server-Sent Events at `/v1/chat/stream` for token-by-token responses, with accurate provider-reported usage.
- **Tool / function calling** — OpenAI-style `tools`/`tool_choice` that work uniformly across OpenAI, Anthropic, and Gemini (RekAI translates the formats per provider).
- **Embeddings** — `/v1/embeddings` across echo, OpenAI, Gemini, and Ollama, with the same routing, caching, BYOK, and cost; a web playground shows vectors and cosine similarity.
- **Model discovery** — `/v1/models` lists every model with its `type` (chat/embedding) and per-model `pricing`, filterable via `?type=`.
- **Response cache** — Redis-backed with an automatic in-memory fallback. Identical requests are served instantly and for free.
- **Cost awareness** — each response carries an estimated USD cost; cumulative spend is exposed at `/v1/usage`.
- **BYOK** — users supply their own provider key per request (`X-Provider-Key`); keys are never persisted.
- **Rate limiting** — per-client token bucket with `Retry-After` and `X-RateLimit-*` headers; oversized bodies are rejected with 413.
- **Observability** — structured text or JSON logging, per-request `X-Request-ID`/`X-Response-Time-Ms`/`X-RekAI-Version` headers, and a Prometheus-style `/metrics` endpoint.
- **OpenAPI** — auto-generated docs at `/docs` and a machine-readable schema at `/openapi.json`.
- **SDKs** — official Python (`rekai-client`) and JS/TS (`@rekai/client`) clients.
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
                         │  │ Providers │  │ ──▶ OpenAI · Anthropic · Gemini · Ollama · Echo
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

### Deploy

One-click on Render (Redis + API + Web) or self-host with Docker — see
[`deploy/`](./deploy).

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

Or smoke-test a running instance end to end:

```bash
scripts/smoke.sh                 # or: make smoke   (BASE_URL=http://localhost:8000)
```

Use a real provider by passing your own key (BYOK):

```bash
curl -s http://localhost:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -H 'X-Provider-Key: sk-...' \
  -d '{"provider":"openai","model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}'
```

Runnable client snippets (curl, Python, JavaScript) live in
[`examples/`](./examples), and there's an installable Python client in
[`packages/python-sdk`](./packages/python-sdk):

```python
from rekai_client import RekAIClient

client = RekAIClient("http://localhost:8000")
print(client.chat("echo", "Hello!").content)
```

## 📦 Monorepo layout

```
RekAI/
├── apps/
│   ├── api/   # FastAPI backend (router, cache, providers, BYOK)
│   └── web/   # Next.js chat UI
├── packages/
│   ├── python-sdk/   # rekai-client: official Python client
│   └── js-sdk/       # @rekai/client: official JS/TS client
├── examples/  # curl / Python / JavaScript snippets
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

## 🛠️ Development

Common tasks are wrapped in a `Makefile`:

```bash
make install   # install API + web dependencies
make check     # lint, type-check, test, and build the web app
make run-api   # run the API   (make run-web for the UI)
make up        # docker compose up --build
```

Run `make help` for the full list. Changes are tracked in [CHANGELOG.md](./CHANGELOG.md).

## 🤝 Contributing

Contributions are welcome! Read [CONTRIBUTING.md](./CONTRIBUTING.md) and our [Code of Conduct](./CODE_OF_CONDUCT.md).

## 📄 License

[MIT](./LICENSE) © RekAI contributors
