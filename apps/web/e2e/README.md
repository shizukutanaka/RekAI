# E2E tests (Playwright)

Regression tests for flows that were hand-verified with ad-hoc Playwright
scripts throughout development, one spec per flow:

- `chat.spec.ts` — sending a chat message renders a reply, and the
  conversation is an accessible live region.
- `gateway-auth.spec.ts` — `REKAI_API_KEYS` locks the app out until a gateway
  key is saved.
- `admin.spec.ts` — the `/admin` runtime key-management UI (wrong key errors,
  correct key manages keys).
- `openai-compat.spec.ts` — the OpenAI-compatible `/v1/chat/completions`
  endpoint (non-streaming and streaming) and the Settings page's drop-in
  base-URL docs.
- `response-notes.spec.ts` — the chat UI's annotations: a semantic cache hit,
  an exact cache hit, an untouched answer, a redacted answer, the usage
  dashboard's semantic-cache breakout, a parked-provider notice before
  sending, no cooldown notice when nothing is parked, and a fallback marker
  (present and absent).
- `stream-error.spec.ts` — a mid-stream error keeps the partial reply on
  screen (marked `· error`) instead of erasing it, same as a user-initiated
  stop (marked `· stopped`).
- `truncation.spec.ts` — a `max_tokens` truncation is called out on both
  streamed and non-streamed replies, an untruncated reply carries no marker,
  and a streamed reply is labelled with the provider that served it.

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
