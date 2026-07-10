# RekAI examples

Runnable snippets that call a local RekAI API (default `http://localhost:8000`).

Start the stack first:

```bash
docker compose up --build      # from the repo root
# or: cd apps/api && uvicorn rekai.main:app --reload
```

All examples default to the keyless `echo` provider, so they work with no
credentials. To use a real provider, set `REKAI_PROVIDER_KEY` (BYOK) and pass a
matching model, e.g. `MODEL=gpt-4o-mini`. If the RekAI deployment itself
requires client auth (`REKAI_API_KEYS`), also set `REKAI_GATEWAY_KEY` — a
separate key from `REKAI_PROVIDER_KEY` above: that one is BYOK for the
upstream provider, this one authenticates you to RekAI.

| File                     | Runtime        | Run                                  |
|--------------------------|----------------|--------------------------------------|
| `curl.sh`                | bash + curl    | `./curl.sh`                          |
| `python/chat.py`         | Python 3 (stdlib) | `python python/chat.py`           |
| `python/stream.py`       | Python 3 (stdlib) | `python python/stream.py`         |
| `python/tools.py`        | Python 3 (stdlib) | `python python/tools.py` (needs a tool-capable model + key) |
| `python/embeddings.py`   | Python 3 (stdlib) | `python python/embeddings.py`     |
| `python/semantic_search.py` | Python 3 (stdlib) | `python python/semantic_search.py "your query"` |
| `javascript/chat.mjs`    | Node 18+       | `node javascript/chat.mjs`           |
| `javascript/embeddings.mjs` | Node 18+    | `node javascript/embeddings.mjs`     |

## Environment variables

| Variable               | Default                 | Meaning                       |
|------------------------|-------------------------|-------------------------------|
| `REKAI_API_URL`        | `http://localhost:8000` | API base URL                  |
| `MODEL`                | `echo`                  | Model to request              |
| `REKAI_PROVIDER_KEY`   | _(unset)_               | BYOK key (`X-Provider-Key`)   |
| `REKAI_GATEWAY_KEY`    | _(unset)_               | Gateway key (`Authorization: Bearer`), only needed if `REKAI_API_KEYS` is set |
