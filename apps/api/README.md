# RekAI API

FastAPI backend for RekAI — the router, cache, provider abstraction and BYOK
handling.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn rekai.main:app --reload
```

- Interactive docs: http://localhost:8000/docs
- OpenAPI schema:   http://localhost:8000/openapi.json
- Health:           http://localhost:8000/health
- Metrics:          http://localhost:8000/metrics

## Endpoints

| Method | Path          | Description                              |
|--------|---------------|------------------------------------------|
| GET    | `/health`     | Liveness, version, providers (+ per-provider readiness), cache type |
| GET    | `/metrics`    | Prometheus-style metrics                 |
| GET    | `/v1/usage`   | Aggregate counters (requests, tokens, cost) |
| GET    | `/v1/models`  | Known models per provider (each tagged `type`: `chat`/`embedding`) |
| POST   | `/v1/chat`    | Chat completion (router + cache + BYOK)  |
| POST   | `/v1/chat/stream` | Streaming chat completion (SSE)      |
| POST   | `/v1/embeddings` | Text embeddings (router + cache + BYOK) |

### Example

```bash
curl -s http://localhost:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"model":"echo","messages":[{"role":"user","content":"hello"}]}'
```

BYOK: pass the upstream provider key with the `X-Provider-Key` header. It is
used transiently and never stored.

## Configuration

All settings are environment variables prefixed `REKAI_` (see `.env.example`).

## Develop

```bash
ruff check . && ruff format --check .
mypy rekai
pytest
```
