# RekAI Web

The Next.js (App Router) chat UI for RekAI.

## Run

```bash
npm install
cp .env.example .env.local   # optional — defaults to http://localhost:8000
npm run dev
```

Open http://localhost:3000.

## Pages

- **/** — chat. Pick a model, send messages. Toggle **Stream** to render
  responses token-by-token via `/v1/chat/stream` (on by default); a **Stop**
  button cancels an in-flight stream. **Options** exposes a system prompt, a
  temperature slider, and a max-tokens cap. The conversation is saved to local
  storage and restored on reload (**Clear** wipes it); **Regenerate** re-runs the
  last turn for a fresh reply. Assistant bubbles show the provider, whether the
  response was cached, token usage, and estimated cost.
- **/usage** — live dashboard of `/v1/usage`: requests, cache hit rate, tokens,
  estimated cost, fallbacks, errors, and a per-provider request breakdown
  (auto-refreshes every 5s).
- **/settings** — store your provider API key (BYOK; lives only in browser local
  storage, sent as the `X-Provider-Key` header) and see per-provider readiness
  (which providers work out of the box vs. need a key). Also has a separate
  **gateway API key** field (sent as `Authorization: Bearer`) for deployments
  where the API has `REKAI_API_KEYS` configured — without it every page would
  get a `401`.
- **/admin** — add or revoke gateway API keys at runtime, without a redeploy
  (`REKAI_DYNAMIC_KEYS_ENABLED`), via the API's `/admin/keys`. Needs a separate
  **admin key** (`REKAI_ADMIN_KEY`, its own local-storage field) — distinct
  from the gateway/provider keys above. Only a key's masked form is ever shown,
  so revoking one needs the raw key typed back in; keep a record of it when you
  add one. If `REKAI_ADMIN_KEY` isn't configured on the API, the page shows a
  clear notice instead of the key-management forms.

## Configuration

| Variable              | Default                 | Description          |
|-----------------------|-------------------------|----------------------|
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | RekAI API base URL   |

## Build

```bash
npm run lint
npm run build
```

## E2E tests

```bash
npm run e2e
```

Playwright specs in `e2e/` — see [`e2e/README.md`](./e2e/README.md).
