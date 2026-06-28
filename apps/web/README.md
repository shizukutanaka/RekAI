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

- **/** — chat. Pick a model, send messages. Assistant bubbles show the
  provider, whether the response was cached, and token usage.
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
