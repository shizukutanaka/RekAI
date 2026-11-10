# Design memo: multilingual Chat UI (i18n)

> **Status: design memo only.** This document proposes how the RekAI web UI
> (`apps/web/`) could become multilingual later. It changes no UI code, adds
> no dependency, and commits to no schedule. It exists so a future
> implementation starts from measured facts about this codebase instead of
> guesses.

## 1. Current state (measured from this repository)

- **Framework:** Next.js `14.2.35` with the App Router, React `18.3.1`
  (`apps/web/package.json`). Every page is a client component
  (`"use client"` at the top of `app/page.tsx`, `app/settings/page.tsx`,
  `app/models/page.tsx`, `app/usage/page.tsx`, `app/admin/page.tsx`,
  `app/embeddings/page.tsx`, and `app/error.tsx`). There is no middleware
  and no locale routing; `next.config.js` has no i18n configuration.
- **No i18n library is installed.** The web app's only runtime dependencies
  are `next`, `react`, and `react-dom`.
- **Language is fixed to English in exactly one obvious place and in
  hundreds of unobvious ones:**
  - `apps/web/app/layout.tsx` renders `<html lang="en">` and the top-bar
    navigation labels (`Chat`, `Embeddings`, `Models`, `Usage`, `Admin`,
    `Settings`).
  - Every other user-visible string is a hard-coded English literal inline
    in the page components: headings, labels, hints, button text,
    `placeholder=` and `aria-label=` attributes, and status/error text
    (e.g. in `app/page.tsx`, ~700 lines, the chat composer placeholder
    `"Type a message… (Enter to send, Shift+Enter for newline)"`;
    `app/settings/page.tsx`, the BYOK key labels and hints;
    `app/error.tsx` / `app/not-found.tsx`, the fallback pages).
  - `apps/web/lib/api.ts` is a data layer, not a string catalog: it defines
    the API client and the local-storage key helpers. It holds no UI copy
    today and should stay that way.
- **Persistence pattern already exists.** The UI already stores
  browser-local preferences in `localStorage` under `rekai.*` keys —
  `rekai.providerKey`, `rekai.gatewayKey`, `rekai.adminKey`
  (`lib/api.ts`) and `rekai.conversation` (`app/page.tsx`). A locale
  preference fits the same pattern and needs no server support.
- **Out of scope for the UI today:** the FastAPI backend also produces
  English error text (e.g. `detail=` strings in `apps/api/rekai/main.py`).
  Those are API contract surface consumed by SDKs and scripts, not UI copy;
  see §5.

## 2. Proposal

### 2.1 Extraction approach

Start **without a new dependency**: a small, typed, in-repo dictionary
module, because the UI is almost entirely client components and the string
count is in the low hundreds — well below the size where an i18n framework
pays for itself.

1. Create `apps/web/lib/i18n/` with:
   - `en.ts` — the source dictionary, typed as
     `Record<key, string>`-style nested objects grouped by route
     (`chat.*`, `settings.*`, `models.*`, `usage.*`, `admin.*`,
     `embeddings.*`, `common.*` for the nav and shared buttons).
   - `ja.ts` — the Japanese dictionary, typed as `typeof en`, so a missing
     key is a **type error at build time** (`tsc` already runs in CI),
     never a silent English fallback in production.
   - `index.tsx` — a `LocaleProvider` (React context) exposing
     `t(key, vars?)` with simple `{name}` interpolation, matching how the
     code already builds strings inline.
2. Replace literals page by page, one route per PR, starting with
   `layout.tsx` (nav + `<html lang>`) and `app/page.tsx` (the chat screen
   users actually live in). Keep each extraction PR mechanical: same
   strings, new indirection, screenshots or e2e text assertions unchanged
   for English.
3. Revisit a framework (`next-intl` is the natural fit for the App Router)
   only if a concrete need appears that the dictionary cannot serve:
   ICU plural/gender rules beyond simple interpolation, per-locale number
   /date formatting at scale (some already exists ad hoc, e.g. cost
   formatting in `app/admin/page.tsx`), or translated server components.
   Adopting it earlier would add middleware, routing, and a dependency to
   solve problems this UI does not yet have.

Do **not** machine-translate strings at runtime or call the gateway itself
to translate its own UI: output would be unreviewed, non-deterministic,
and would cost users provider tokens to render a settings page.

### 2.2 Language set

- **First: `en` (source + fallback) and `ja`.** English is the existing
  copy; Japanese is the maintainer's language and the largest known
  user group not served today.
- Add a language only when a complete dictionary for it exists (the
  `typeof en` typing makes "complete" checkable). Partial languages ship
  as English with a translated nav, **not** as a half-translated UI —
  mixed-language screens are worse than honest English.
- Model names, provider names, API paths, header names
  (`X-Provider-Key`, `Authorization: Bearer`), and environment variable
  names (`REKAI_API_KEYS`, …) are **never translated** — they are
  identifiers, not prose.

### 2.3 Detection and storage

Resolution order on first load, all client-side:

1. Stored choice: `localStorage` key `rekai.locale`
   (`en` | `ja`), following the existing `rekai.*` convention in
   `lib/api.ts`. A small getter/setter pair lives next to the existing
   key helpers.
2. Browser hint: `navigator.language` (`ja*` → `ja`, anything else →
   `en`).
3. Default: `en`.

Plus a manual language switcher in the top bar in `layout.tsx` — the one
place every route shares — which writes `rekai.locale` and updates
`<html lang>` to match (screen readers and browser translation key off
that attribute; leaving it at `en` on a Japanese UI would be a bug).

Explicit non-goals for detection/storage:

- **No locale URL prefix** (`/ja/chat`, …) for now. RekAI is self-hosted
  per user/team, the UI is an app shell rather than shareable content,
  and locale routing would complicate the Docker/`NEXT_PUBLIC_API_URL`
  build-time baking for no user-visible gain. Revisit only if public,
  linkable, per-language pages ever exist.
- **No server-side storage.** The locale never goes to the API, is not a
  header, and is not tied to the gateway/provider keys. It is a
  per-browser display preference, exactly like the existing stored keys.
- **No `Accept-Language` negotiation on the API** (see §5).

### 2.4 Numbers, dates, and costs

UI-formatted values (USD cost, token counts, timestamps on the Usage and
Admin pages) should move to `Intl.NumberFormat` / `Intl.DateTimeFormat`
with the active locale as dictionaries land — this is separate from
string translation and easy to forget, so it is called out here as part
of "done" for each route. The underlying values and the API's formats do
not change.

## 3. Suggested implementation order (for a future change)

1. `lib/i18n/` scaffolding + `LocaleProvider` + top-bar switcher +
   `layout.tsx` strings + `<html lang>` wiring. English output identical.
2. Chat (`app/page.tsx`) — the highest-traffic screen.
3. Settings, Models, Usage.
4. Admin, Embeddings, `error.tsx`, `not-found.tsx`.
5. `Intl.*` formatting pass for cost/date display.
6. Tests: dictionary key-parity is enforced by the type system; add one
   vitest that renders a page in `ja` and asserts a known translated
   string, and extend Playwright e2e only for the switcher itself.

## 4. What NOT to do

- Do not add an i18n framework, middleware, or locale routing "just in
  case" (§2.1, §2.3).
- Do not translate identifiers: model/provider names, endpoints, headers,
  env vars, code samples in hints.
- Do not ship a partial language as if complete (§2.2).
- Do not store or transmit the locale server-side; do not couple it to
  BYOK/gateway/admin keys.
- Do not runtime-machine-translate UI copy with an LLM.
- Do not fork the UI per language or duplicate components per locale —
  one component tree, dictionary-driven.

## 5. Explicitly out of scope

- **API/backend messages.** FastAPI error `detail` strings and the OpenAPI
  docs stay English: they are a machine-consumed contract (SDKs, tests,
  and scripts match on them), and changing them is an API change, not a
  UI change. If human-facing API errors are ever localized, that is a
  separate design (request-scoped, opt-in) and must not ride along with
  this UI work.
- **README/docs translation.** This memo covers the Chat UI only.
- **Chat content.** Messages users send and model replies are never
  translated by RekAI; language of conversation is between the user and
  the model.
