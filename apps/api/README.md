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
| GET    | `/`           | Service banner (name, version, links to `/docs`, `/health`) |
| GET    | `/health`     | Liveness, version, providers (+ per-provider readiness), cache type |
| GET    | `/metrics`    | Prometheus-style metrics                 |
| GET    | `/v1/usage`   | Aggregate counters (requests, tokens, cost) |
| GET    | `/v1/models`  | Known models per provider (each tagged `type`; filter with `?type=chat\|embedding`) |
| POST   | `/v1/chat`    | Chat completion (router + cache + BYOK)  |
| POST   | `/v1/chat/stream` | Streaming chat completion (SSE)      |
| POST   | `/v1/embeddings` | Text embeddings (router + cache + BYOK) |
| GET/POST | `/admin/keys` | List / add runtime API keys (needs `REKAI_ADMIN_KEY`) |
| DELETE | `/admin/keys/{key}` | Revoke a runtime API key |

### Example

```bash
curl -s http://localhost:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"model":"echo","messages":[{"role":"user","content":"hello"}]}'
```

Auth: set `REKAI_API_KEYS` (comma-separated) to require
`Authorization: Bearer <key>` on `/v1/*` (constant-time check; open by default).
Set `REKAI_DYNAMIC_KEYS_ENABLED=true` and `REKAI_ADMIN_KEY` to also manage keys
at runtime via `/admin/keys` instead of a redeploy — see
[docs/architecture.md](../../docs/architecture.md#dynamic-key-management).

BYOK: pass the upstream provider key with the `X-Provider-Key` header. It is
used transiently and never stored.

Idempotency: pass a unique `Idempotency-Key` header to safely retry a `POST` —
a repeat with the same key replays the first response (`Idempotent-Replay: true`)
instead of processing again.

## Configuration

All settings are environment variables prefixed `REKAI_` (see `.env.example`).

## Develop

```bash
ruff check . && ruff format --check .
mypy rekai
pytest
```
