# Deploying RekAI

RekAI is a standard 3-service stack: **Redis**, the **API** (FastAPI), and the
**Web** UI (Next.js). Any platform that can run Docker images works.

> One thing to know: the web UI bakes `NEXT_PUBLIC_API_URL` into its bundle at
> **build time**, so the API's public URL must be set when the web image is
> built — not just at runtime. The web `Dockerfile` accepts it as a build arg
> (`--build-arg NEXT_PUBLIC_API_URL=https://api.example.com`).

## Option 1 — Render (one blueprint)

[`render.yaml`](./render.yaml) provisions all three services.

1. Push this repo to GitHub.
2. In Render, **New + → Blueprint** and select the repo.
3. Render reads `render.yaml`, creates `rekai-redis`, `rekai-api`, `rekai-web`.
4. If your `rekai-api` URL differs from `https://rekai-api.onrender.com`, update
   `NEXT_PUBLIC_API_URL` on the `rekai-web` service and redeploy it.

To use a real provider without BYOK, add e.g. `REKAI_OPENAI_API_KEY` to the
`rekai-api` service (uncomment it in the blueprint or set it in the dashboard).

## Option 2 — Self-host with Docker Compose

On any VM with Docker:

```bash
git clone https://github.com/shizukutanaka/RekAI.git
cd RekAI
docker compose up --build -d
```

This starts Redis, the API (`:8000`), and the Web UI (`:3000`). Server-side
provider keys go in `apps/api/.env` (`cp apps/api/.env.example apps/api/.env`),
which compose reads as the api service's `env_file`; it is git- and
docker-ignored, so nothing is committed or baked into an image. Put a TLS
reverse proxy (Caddy, nginx, Traefik) in front. If the web UI is served from a
different host than the API, rebuild the web image with the right API URL:

```bash
docker build -t rekai-web \
  --build-arg NEXT_PUBLIC_API_URL=https://api.example.com ./apps/web
```

## Option 3 — Other platforms (Fly.io, Railway, Cloud Run, …)

Deploy two web services from the per-app Dockerfiles plus a managed Redis:

- **API** — build `apps/api/Dockerfile` (context `apps/api`); set
  `REKAI_REDIS_URL`, expose port `8000`, health check `/health`.
- **Web** — build `apps/web/Dockerfile` (context `apps/web`) with
  `--build-arg NEXT_PUBLIC_API_URL=<api public url>`; expose port `3000`.
- **Redis** — any managed instance; pass its URL as `REKAI_REDIS_URL` to the API.

See [`apps/api/.env.example`](../apps/api/.env.example) for all API settings.
