# CLAUDE.md — agent conventions for this repo

Shared rules for any AI agent (Claude Opus / Sonnet / Haiku, etc.) working on
RekAI. Model-specific task assignments live in
[`docs/ai/instructions-opus.md`](./docs/ai/instructions-opus.md) and
[`docs/ai/instructions-sonnet.md`](./docs/ai/instructions-sonnet.md).

## What this repo is

A self-hostable AI gateway (FastAPI, `apps/api/rekai/`) with a Next.js UI
(`apps/web/`), Python/JS SDKs (`packages/`), and runnable examples
(`examples/`). Read [`docs/architecture.md`](./docs/architecture.md) before
touching the request pipeline — it is accurate and kept up to date.

## Verification gates (run before every commit)

**Run CI's exact commands, not narrower ones.** These are copied from
`.github/workflows/ci.yml`; if they drift apart, CI is the authority and this
file is the bug. Run each on its own line rather than chaining with `&&` — a
chain tells you it failed but not which gate did.

API — all four must pass, from `apps/api/`:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .      # NOT `rekai tests`: CI checks README code blocks too
.venv/bin/mypy rekai
.venv/bin/python -m pytest -q        # 680+ tests, ~9s
```

**A green local run does not predict CI.** A local venv pins the dependencies it
resolved the day it was built; CI resolves them fresh every run. That gap kept
this repo's CI red from the day the workflow was installed until 2026-09-02 —
mypy died inside a *newer numpy's* stubs, on a tree whose author had honestly
reported "mypy … 550 passed" locally. When a change touches typing, dependency
metadata, or anything version-sensitive, build a scratch venv on the Python CI
uses and run the suite there too:

```bash
python3.12 -m venv /tmp/ci && /tmp/ci/bin/pip install -e ".[dev]"
```

Web — from `apps/web/`:

```bash
npx tsc --noEmit -p . && npm run lint && npx vitest run && npm run build
```

SDKs — `packages/python-sdk`: `.venv/bin/python -m pytest -q`, then
`.venv/bin/ruff check .` **and `.venv/bin/ruff format --check .`** (CI runs the
format check here too, and it covers the README's code blocks);
`packages/js-sdk`: `node --test`.

E2E (optional but preferred for UI-visible changes) — from `apps/web/`:
`PLAYWRIGHT_CHROMIUM_PATH=/opt/pw-browsers/chromium npx playwright test`
(specs start their own API on port 8090; serial by design — see
`apps/web/e2e/README.md`). Never run `playwright install`.

Beyond tests: **live-verify** new behavior (uvicorn + curl, or a Playwright
page load) before committing. Every feature commit message states how it was
verified.

## Working conventions

- One feature/fix per commit; update `CHANGELOG.md` (`[Unreleased]`) in the
  same commit. Docs live next to the code they describe.
- Match existing patterns before inventing new ones. Established idioms:
  - **Redis-when-configured, process-local otherwise, fail-open on Redis
    errors** (cache, rate limiter, cooldown, dynamic keys, metrics store).
  - **Bounded data structures** — anything keyed by client id or request data
    must have a cap + eviction (see `Metrics.max_tracked_clients`,
    `RateLimiter.max_buckets`).
  - **Providers are long-lived singletons** with a persistent
    `httpx.AsyncClient` via `Provider._client()` (loop-keyed). Don't create
    per-request clients.
  - Epoch-aligned fixed windows: `int(now / window)` (rate limiter, budget
    windows).
  - Tests use `TestClient(create_app(Settings(environment="test",
    default_provider="echo")))` and monkeypatch `httpx.AsyncClient` with fake
    clients for provider payload capture.

## Git constraints specific to this environment

- Branch: work stays on the designated `claude/...` branch; never push
  elsewhere.
- **`.github/workflows/` cannot be pushed** (the GitHub App token lacks
  `workflows` permission), and **tags cannot be pushed** either. Keep any
  workflow-touching commit isolated as the *last local commit*: before pushing
  other work, `git stash push -u -- .github/workflows/` + `git reset --soft
  <last-pushed>` it off, push, then re-commit it at the tip. A maintainer must
  push `ci.yml` and tags manually (or grant the permission).
- Committer identity must be `Claude <noreply@anthropic.com>`; if a stop-hook
  flags the tip commit, run `git config user.email noreply@anthropic.com &&
  git config user.name Claude && git commit --amend --no-edit --reset-author`.
- Docker daemon is unavailable in the sandbox — validate compose changes with
  `docker compose config -q` and say explicitly when an image was not
  build-verified.
