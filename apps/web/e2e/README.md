# E2E tests (Playwright)

Regression tests for flows that were hand-verified with ad-hoc Playwright
scripts throughout development: sending a chat message, gateway auth
(`REKAI_API_KEYS`) locking the app out until a gateway key is saved, and the
`/admin` runtime key-management UI.

## Running locally

```bash
npm install
npm run e2e
```

`playwright.config.ts`'s `webServer` builds and starts the web app on a fixed
port (3010), with `NEXT_PUBLIC_API_URL` baked to `http://localhost:8090` (also
fixed — see `helpers/api-server.ts`). Each spec starts and stops its own API
process on that port with the `REKAI_*` env it needs (e.g. `REKAI_API_KEYS`,
`REKAI_ADMIN_KEY`), so specs run **serially** (`workers: 1` — they can't share
one API process with different auth configs at once).

Requires the API's virtualenv to already exist at `apps/api/.venv` (see the
root README's `make install`).

## Adding a spec

Call `startApi({...})` in `test.beforeAll` with whatever `REKAI_*` env the
scenario needs (defaults: open auth, `REKAI_DEFAULT_PROVIDER=echo`, rate
limiting off), and `stopApi(api)` in `test.afterAll`.
