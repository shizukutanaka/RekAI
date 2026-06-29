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
  storage and restored on
  reload (**Clear** wipes it). Assistant bubbles show the provider, whether the
  response was cached, token usage, and estimated cost.
- **/usage** — live dashboard of `/v1/usage`: requests, cache hit rate, tokens,
  estimated cost, fallbacks, errors, and a per-provider request breakdown
  (auto-refreshes every 5s).
- **/settings** — store your provider API key (BYOK; lives only in browser local
  storage, sent as the `X-Provider-Key` header) and see per-provider readiness
  (which providers work out of the box vs. need a key).

## Configuration

| Variable              | Default                 | Description          |
|-----------------------|-------------------------|----------------------|
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | RekAI API base URL   |

## Build

```bash
npm run lint
npm run build
```
