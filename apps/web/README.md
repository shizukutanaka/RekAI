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
  responses token-by-token via `/v1/chat/stream` (on by default). Assistant
  bubbles show the provider, whether the response was cached, and token usage.
- **/usage** — live dashboard of `/v1/usage`: requests, cache hit rate, tokens,
  estimated cost, fallbacks, errors, and a per-provider request breakdown
  (auto-refreshes every 5s).
- **/settings** — store your provider API key (BYOK). The key lives only in
  browser local storage and is sent as the `X-Provider-Key` header.

## Configuration

| Variable              | Default                 | Description          |
|-----------------------|-------------------------|----------------------|
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | RekAI API base URL   |

## Build

```bash
npm run lint
npm run build
```
