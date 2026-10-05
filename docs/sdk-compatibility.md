# Server ↔ SDK compatibility

Every route the gateway serves and which surface reaches it — the Python SDK
(`rekai_client`, sync and async), the JS SDK (`@rekai/sdk`), and the web UI.
Server-only routes are listed so a missing SDK method reads as a deliberate
boundary, not a gap to file.

## Data-plane routes

| Route | Purpose | Python SDK | JS SDK | Idempotency-Key | Notes |
|---|---|---|---|---|---|
| `POST /v1/chat` | Chat completion (RekAI surface) | `chat()` | `chat()` | accepted | |
| `POST /v1/chat/stream` | Same, SSE | `stream()` | `stream()` | accepted | SDKs retry only pre-headers; mid-stream stalls are watchdog-aborted |
| `POST /v1/embeddings` | Embeddings | `embeddings()` | `embeddings()` | accepted | |
| `POST /v1/moderations` | Moderation pass-through | `moderations()` | `moderations()` | accepted | |
| `POST /v1/chat/completions` | OpenAI drop-in | — | — | accepted | Use the OpenAI SDK pointed at the gateway; `stream_options.include_usage` honored |
| `POST /v1/messages` | Anthropic drop-in | — | — | accepted | Use the Anthropic SDK; thinking/server-tool blocks round-trip verbatim |
| `POST /v1/messages/count_tokens` | Anthropic token counting | — | — | — | Read-only; no side effects to deduplicate |
| `GET /v1/models` | Model registry | `models()` | `models()` | — | |
| `GET /v1/models/{model_id}` | Single model | — | — | — | Registry detail; SDKs fetch the full list and filter client-side |

## System and admin routes (server-only by design)

| Route | Purpose | Why no SDK method |
|---|---|---|
| `GET /` | Service info | deployment metadata |
| `GET /health` | Provider readiness/parking | `health()` exists on both SDKs — listed here for completeness; it is the one system route they do expose |
| `GET /metrics` | Prometheus text | scraped by Prometheus, not by SDK clients |
| `GET /v1/usage` | Usage summary | `usage()` on both SDKs |
| `GET /admin/usage` | Fleet-wide usage (admin key) | admin surface — use `curl` or the web UI (`/admin`) |
| `GET/POST/DELETE /admin/keys` | Dynamic API-key management | admin surface — web UI (`/admin`) or `curl` |

## Wire-level guarantees both SDKs rely on

- **Error envelope**: non-2xx returns `{"error": {...}}` (or `{"detail": ...}`
  on validation errors); SDKs surface the status code on the raised error.
- **SSE contract**: `data: {json}` lines terminated by `data: [DONE]`; event
  kinds the SDKs parse are `delta`, `usage`, `tool_calls`, `annotations`,
  `refusal`, `thinking`, `error`.
- **Idempotent retries**: `Idempotency-Key` dedupes server-side for the TTL;
  SDKs auto-generate keys (`rekai-sdk-` prefix) when `max_retries > 0` and
  honor an explicit key when given.
- **Auth**: `Authorization: Bearer <gateway key>` for the gateway itself;
  `X-Provider-Key` (JS SDK `providerKey`, Python `provider_key`) carries BYOK
  upstream credentials through.
