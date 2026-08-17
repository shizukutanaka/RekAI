# Changelog

All notable changes to RekAI are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
- **A non-ASCII `Authorization` header no longer crashes the auth gate.**
  `secrets.compare_digest` rejects a `str` containing any non-ASCII character
  with `TypeError`, and the presented token is attacker-controlled — so
  `Authorization: Bearer ké` raised out of the auth middleware as an unhandled
  **500 instead of a 401**, on `/v1/*` and `/admin/*` alike. Three things made
  that more than cosmetic: auth runs *before* the rate limiter, so these
  requests consumed **no rate-limit budget**; the exception escaped before
  `metrics.record_error`, so they were **invisible in `rekai_errors_*`**
  (measured: `errors_total` 1 after 9 crashes); and each one logged a full
  stack trace. An unauthenticated client could therefore generate unbounded,
  unmetered, uncounted 500s and log volume by changing one character. Keys are
  now compared as bytes (`surrogatepass`, so a lone surrogate can't reintroduce
  it), which keeps the comparison constant-time and turns every malformed
  credential into an ordinary 401.
- **The in-process rate limiter no longer degrades under the flood it exists to
  stop.** Its bucket cap was enforced by reclaiming only *fully-refilled*
  buckets — which reclaims nothing during a flood of distinct client ids, since
  every bucket is then mid-refill. So the dict grew past `max_buckets` without
  limit, and because the reclaim scan ran on every request once at the cap, cost
  grew quadratically: **8000 distinct ids against the default 60-per-60s config
  took 4.4 s and left 1612 buckets under a 1000 cap** (an algorithmic-complexity
  attack, Crosby & Wallach, USENIX Security 2003 — the component meant to stop
  abuse amplifying it). Reclaim now falls back to evicting the buckets closest
  to full, in amortized batches: the same workload is **44 ms and exactly 1000
  buckets**, ~288× faster with the cap actually enforced. Eviction order is
  deliberate — evicting a bucket resets that client to full capacity, so
  discarding the *least*-throttled gives away the least budget and can't be used
  by an attacker to flood their own exhausted bucket out and reset their limit.
- **Streaming cost/budget estimation is script-aware — CJK no longer counts as
  ~1 token.** When a provider streams without reporting usage, RekAI estimates
  the token count, and that estimate feeds both `cost_usd` and the per-client
  budget cap (`REKAI_CLIENT_BUDGET_USD`). The estimator was `len(text.split())`
  — a whitespace word count — which undercounts Latin text ~30% and, for scripts
  without spaces (Japanese, Chinese), collapsed an entire reply to ~1 token: a
  100×+ undercount that let a CJK-language app run past its spend cap
  effectively unmetered. It now counts CJK/kana/Hangul characters ~1 token each
  and the rest at OpenAI's ~4-chars-per-token rule, landing within ~15% of
  `o200k_base` across English, Japanese, Chinese, and Korean and erring slightly
  high (the safe direction for a cap). Still a dependency-free heuristic — an
  exact tokenizer needs a per-model vocab download a self-hosted deployment
  can't assume, and providers that report exact usage never hit this path.
- **Output redaction now covers streaming.** `REKAI_OUTPUT_REDACTION_ENABLED`
  scrubbed `/v1/chat` but not `/v1/chat/stream` — the endpoint a chat UI
  actually uses — so a deployment that enabled redaction for compliance got no
  protection on most of its traffic. This was documented as unavoidable: a
  secret can straddle two SSE chunks (`sk-aaaa` | `aaaa…` matches nothing in
  either half) and catching it looked to require buffering the whole reply. It
  doesn't. A match can only *begin* at one of a small set of literal prefixes
  (`sk-`, `AKIA`, `ghp_`, `-----BEGIN`, …), so `guardrails.StreamRedactor` holds
  back only the text from the last such prefix onward: ordinary prose is delayed
  by 9 characters, and the buffer grows only once something resembling a secret
  appears. A stream reports what it caught in its terminal summary event
  (`"redacted": [...]`), since headers are long gone by then. A regression test
  splits each of the eight secret formats at *every* byte boundary and asserts
  neither the secret nor its tail survives, and that streamed output is
  byte-identical to the non-streaming path.
- **Output redaction now runs before the response is cached** — the documented
  promise ("redaction happens before the response is cached or stored for
  `Idempotency-Key` replay") was only half true. The idempotency store was
  scrubbed, but the response cache and the semantic cache were written inside
  `handle_chat`, *before* the edge redacted, so a secret the model echoed back
  was persisted verbatim — in Redis, in plaintext, for the whole
  `REKAI_CACHE_TTL_SECONDS`. The scrub moved into `service.handle_chat` ahead of
  every store, and `ChatResponse` gained a `redacted: ["<pattern>", …]` field so
  a cache hit or an idempotent replay still reports `X-Redacted` (previously the
  replay path emitted no header at all). The edge keeps a re-scan as a backstop
  for entries cached before the setting was switched on. Only affects
  deployments with `REKAI_OUTPUT_REDACTION_ENABLED=true`.
- **Idempotency records are scoped per client** — the store key was
  `sha256(Idempotency-Key)` alone, a single global namespace. Since idempotency
  keys are caller-chosen and collide constantly (`req-1`), tenant B sending
  tenant A's key with a matching body was served **A's stored response**, and
  claiming the in-progress sentinel first would 409 A out of its own key. The
  key now hashes the client id (masked API-key id under gateway auth, else the
  client IP) together with the header value, length-prefixed so no
  `(client, key)` split can be rearranged into another's digest — the same
  per-API-key scoping Stripe uses. `idempotency.claim/complete/release` take a
  `client_id` argument.
- **`REKAI_ALLOWED_PROVIDERS` — operator control over which providers a request
  may reach** (new, empty = unrestricted, so no behavior change by default). An
  operator holding server-side keys for several providers had no way to restrict
  tenants to a subset: any authenticated caller could name any registered
  provider (via `provider`, a model prefix, or request-level `fallbacks`) and
  bill the operator's key for it. `router.ensure_allowed` now gates all three
  paths with a 403; `REKAI_DEFAULT_PROVIDER` is always allowed. Request-level
  `fallbacks` reject off-list targets outright rather than skipping them
  silently, so a caller can't end up believing it has a chain it doesn't.
- **Per-tenant spend is no longer visible to other tenants** — `usage_by_client`
  names each tenant and what it spent, and both endpoints carrying it were
  readable by the wrong audience: `/v1/usage` returned the full cross-tenant map
  to any valid gateway key, and `/metrics` (open by default, so Prometheus can
  scrape) emitted `rekai_client_*{client="key:…"}` to *unauthenticated* callers.
  `/v1/usage` is now a tenant view — aggregate counters stay fleet-wide,
  `usage_by_client` holds only the calling key's row; `/metrics` keeps its
  operational series open but emits the `rekai_client_*` series only to an
  authenticated scrape. **Breaking for unauthenticated Prometheus setups that
  graph per-client series** — authenticate the scrape (any valid gateway key) to
  restore them. New **`GET /admin/usage`** returns the full fleet breakdown under
  `REKAI_ADMIN_KEY`, with the same rate limiting and audit logging as
  `/admin/keys`. Nothing changes when no gateway auth is configured.
- **Semantic cache correctness** (opt-in feature; five defects, all of which
  could return a *wrong answer*):
  - **Bucket was under-specified.** Entries were partitioned by
    `f"{provider}:{model}:{temperature}:{max_tokens}"` — omitting `tools`,
    `tool_choice`, `response_format`, and `cache_control`, i.e. exactly the
    collisions `cache_key`'s own comments warn about. A JSON-mode request could
    be answered from a prose entry. Now `cache.semantic_bucket`, defined beside
    `cache_key` as its payload minus `messages`.
  - **Entries were shared across tenants.** A semantic hit answers a prompt the
    caller never sent, so the bucket now includes the client id. (The exact
    cache still shares: a hit there requires the caller to have sent the
    identical prompt itself.)
  - **`REKAI_SEMANTIC_CACHE_MAX_ENTRIES` was dead config** — declared,
    documented, and never passed to `SemanticCache()`, which used its own
    default of 1000. Now applied in `create_app`.
  - **Entries never expired**, while the exact cache honors
    `REKAI_CACHE_TTL_SECONDS`. They now share that TTL.
  - **The embedding call was unmetered** — a real upstream call per request,
    billed to the operator's key, invisible in `/v1/usage`. Now counted.
- **`REKAI_SEMANTIC_CACHE_MODEL` no longer defaults to `echo`** (**breaking** for
  anyone who enabled the semantic cache without setting a model: startup now
  fails with an explanatory error instead of silently using `echo`). `echo`
  embeddings are a 16-dimension SHA-256 slice, so every vector is in the positive
  orthant, unrelated prompts sit around 0.78 cosine, and ~12% of random pairs
  clear the 0.85 default threshold — a false-hit generator, not a cache.
  Explicitly setting it to `echo` still works for tests, with a loud warning.
- **Web dev-tooling audit cleanup** — bumped `vitest` 2 → 4, clearing the
  critical advisory in its bundled `vite`/`esbuild`/`vite-node`/`@vitest/mocker`
  chain, and ran `npm audit fix` for a transitive `brace-expansion` fix — from
  11 advisories (1 critical) down to 5. The remaining five are the Next.js
  framework advisories, which only a `next` 14 → 16 major resolves; tracked as
  a dedicated follow-up (S-3b) since a framework major needs its own migration
  and full re-verification. All web gates (tsc/lint/vitest/build/E2E) pass on
  vitest 4.

### Fixed
- **W3C Trace Context conformance.** `traceparent` was validated with
  `int(value, 16)`, far more permissive than the spec's `HEXDIGLC` (lowercase
  hex only): a leading sign, underscore digit separators, and surrounding ASCII
  whitespace all passed, so values like `+bf92…` and `4bf9…47_6` were accepted
  as trace ids — then formatted back into the response header *and* the outbound
  provider header, i.e. RekAI emitted a `traceparent` that a conforming parser
  must reject, silently breaking the correlation the header exists to provide.
  (Not header injection: a raw CR/LF can't reach a single header value, since
  the HTTP parser splits on it first.) Now validated by regex. A **future
  version is parsed rather than rejected**, per the spec's forward-compatibility
  rule — the previous code accepted only `00`, so it would have restarted every
  trace the day the spec advanced, and an existing test had encoded that as
  intended. `ff` stays reserved and rejected.

### Added
- **`REKAI_REQUEST_DEADLINE_SECONDS` — a total budget for one request.**
  `REKAI_REQUEST_TIMEOUT_SECONDS` reads like a request bound and is not one: it
  caps a *single* outbound call, which `REKAI_RETRY_MAX_ATTEMPTS` multiplies and
  the fallback chain multiplies again. Measured against a hung upstream with a
  1.0 s per-call bound: **6 upstream calls and 6.04 s of client wait** — scaled
  to the shipped defaults (60 s, 2 attempts, a 3-target chain) that is **~6
  minutes** holding a connection and a concurrency slot for a request whose
  "timeout" is one minute. The new setting is the missing half — the split Envoy
  draws between `route.timeout` and `retry_policy.per_try_timeout`, and that
  LiteLLM/Portkey expose as two settings. It is enforced before starting each
  fallback target, around each attempt (so one hung upstream cannot overrun the
  budget by itself), and before each backoff sleep (a sleep that would leave no
  time to actually retry is skipped, and the real upstream error raised
  instead). Exceeding it returns **504**; when a genuine upstream failure is in
  hand that is surfaced instead, so the client sees the cause and not the
  budget. `0` (the default) keeps today's unlimited behavior. Streaming is
  exempt — a stream's duration is the length of the answer, not a fault, and
  `REKAI_MAX_CONCURRENT_REQUESTS` already bounds occupancy there.
- **`tracestate` is now propagated.** `traceparent`'s companion header carries
  vendor state (sampling decisions, a vendor's own trace id) and was dropped
  entirely — the spec pairs the two, and a gateway is the hop every call
  crosses, so this stranded that state on every request. It is now forwarded to
  providers alongside `traceparent` and echoed to the client. Since it is
  attacker-controlled and goes back out in a header, it is validated rather than
  passed through verbatim: printable ASCII only, at most 32 list members,
  truncated to 512 bytes on a member boundary.
- **`finish_reason` now comes from the provider instead of being synthesised.**
  All five backends report why generation stopped — OpenAI `finish_reason`,
  Anthropic `stop_reason`, Gemini `finishReason`, Ollama `done_reason` — and
  RekAI parsed none of them, emitting `"tool_calls" if tool_calls else "stop"`
  at the edge. So an answer **cut off by `max_tokens` was reported as a normal
  completion**: the standard "retry with a larger budget when
  `finish_reason == 'length'`" pattern could never fire, and the truncated
  answer was cached and replayed to later callers as if it were whole. The four
  vocabularies are normalized onto OpenAI's (`stop` / `length` / `tool_calls` /
  `content_filter`) and carried through `ProviderResult`, `StreamEvent`,
  `ChatResponse` (new nullable `finish_reason` field), both streaming paths, and
  the OpenAI-compatible translation. Two wrinkles are handled rather than passed
  through: Gemini says `STOP` even when emitting a `functionCall` (a tool call is
  inferred from the parts), and Anthropic's forced-tool JSON emulation says
  `tool_use` (rewritten to `stop`, since the caller never sees a tool call).
  `null` means the provider said nothing — also how responses cached before this
  field existed read, and the OpenAI endpoint falls back to the old derivation
  there. Both SDKs expose it.
- **`response_format` is now honored by every provider.** RekAI advertised JSON
  mode in its OpenAPI schema and README, but Anthropic and Ollama accepted the
  field and dropped it with only a debug log — a caller who asked for JSON got
  prose and no signal, which breaks the one promise a gateway makes. Neither
  provider lacked the capability:
  - **Ollama** takes a top-level `format`: `"json"` for free-form JSON, or the
    JSON schema itself, which it uses for *constrained decoding* so the output
    conforms by construction. The old log line said "unsupported by the ollama
    provider", which was simply untrue.
  - **Anthropic** has no `response_format` at all, so RekAI uses its documented
    route — forced tool use: inject one tool whose `input_schema` is the
    requested shape, pin `tool_choice` to it, then **unwrap the resulting
    `tool_use` block back into JSON `content`** and suppress the tool call, so
    the response looks like OpenAI's JSON mode. Streaming included: the forced
    tool's `input_json_delta` fragments are emitted as text deltas.

  Two documented limits: a `json_object` request has no schema, so the injected
  tool uses a permissive `{"type": "object"}`; and if the caller sends their own
  `tools` alongside `response_format`, the tools win — only one tool can be
  forced, and silently disabling tools someone explicitly asked for is worse
  than leaving the ambiguity with them.
- **Semantic cache: meaning-flipping edits can no longer produce a hit.** A
  cosine threshold cannot catch the failure mode that matters most, because the
  vectors really are that close: "is aspirin safe during pregnancy" vs "is
  aspirin **not** safe during pregnancy", or "convert **5** USD" vs "convert
  **500** USD", are near-identical to an embedding model and opposite questions
  to a user. Candidates are now rejected when their negation count or their
  ordered numeric literals differ from the query's, however similar the
  embeddings — the check *GPTCache* (arXiv:2311.13133) delegates to a second
  model, minus the second model call. It can only turn a hit into a miss (a
  wrong miss costs one upstream call; a wrong hit answers a question nobody
  asked), and prompts with neither feature — most conversational traffic — are
  unaffected. Only the digest is retained, never the prompt text.
- **Semantic cache lookups are ~60× faster, and their cost is now measured** —
  the similarity scan called a cosine that re-derived *both* vectors' norms on
  every entry, including the stored vector's, once per entry per lookup.
  Measured on the default 1000-entry bound with 1536-dim embeddings, per lookup:
  **~124 ms → ~48 ms** by unit-normalizing on insert so a comparison is a bare
  dot product, **→ ~2.1 ms** when NumPy is installed. NumPy is optional (a dev
  extra so the suite tests both paths; the pure-Python path stays the
  reference), not a runtime dependency. New
  `rekai_semantic_cache_lookup_seconds{result}` histogram, because the residual
  cost is paid on every request including misses — at ~48 ms a pure-Python
  deployment fronting a fast provider can spend more than the cache saves, and
  that should be visible rather than inferred.
- **Semantic cache: entries from a different embedding model can no longer
  match.** Changing `REKAI_SEMANTIC_CACHE_MODEL` leaves old-dimension entries in
  the process-local store; they scored `0.0`, which under a threshold of `0.0`
  counts as a hit (`0.0 >= 0.0`). They are now skipped outright. Also, a
  vector's similarity to itself came out as `0.9999999999999998` from float
  accumulation, so a threshold of exactly `1.0` — "only an identical embedding
  may hit" — could never match anything; similarity is now rounded at 1e-12,
  well above the noise floor and far below any meaningful discrimination.
- **Semantic cache hits disclose their similarity** — a hit that answers a
  *different* prompt arrived as `cached: true`, identical to an exact hit, and
  landed in the same `rekai_cache_hits_total` counter, so neither a caller nor
  an operator could tell the two apart. Responses now carry
  `cache_similarity` (plus an `X-Cache-Similarity` header), null on a miss and
  on an exact hit so a value is exactly the signal that an approximate match was
  used; `rekai_semantic_cache_hits_total` / `UsageSummary.semantic_cache_hits_total`
  count the subset. Both SDKs expose the new fields.
- **`REKAI_MAX_CONCURRENT_REQUESTS` — a cap on in-flight `/v1/*` requests**
  (opt-in, `0` = unlimited, so no behavior change by default). The rate limiter
  bounds *arrivals*; nothing bounded *occupancy*, which for an LLM gateway is a
  different quantity — 60 requests/minute is satisfiable by 60 concurrent
  60-second streams. And since `httpx`'s read timeout resets per chunk, a
  slow-trickling upstream could hold a streaming request open indefinitely
  without ever reaching `REKAI_REQUEST_TIMEOUT_SECONDS`. Excess requests get 429
  + `Retry-After` (`error: "concurrency_limit"`) rather than queueing. Pure-ASGI
  and wrapped around the whole app, so a slot is held until the last byte of a
  streamed body is sent — a `BaseHTTPMiddleware` dispatch would release it
  before the first token. `/health` and `/metrics` are outside the cap, so they
  stay answerable exactly when the gateway is saturated.
- **`/health` can report `degraded`** — `status` was typed `Literal["ok"]`, so
  the endpoint structurally could not signal a problem, even though the cooldown
  and circuit-breaker machinery already knew which providers were parked (the
  only external evidence was `rekai_cooldowns_total`, which says something
  happened, not what is happening). It now returns `degraded` while any provider
  is in cooldown, with `parked_providers` giving each one's remaining seconds.
  Still HTTP 200 when degraded — the gateway is serving, and failing a liveness
  probe over one parked provider would take down a working deployment. Still no
  I/O: no upstream probe (an unauthenticated request amplifier) and no Redis
  ping, so `parked_providers` is the local worker's view.
- **Errors are dimensioned** — `record_error()` took no arguments, so
  `rekai_errors_total` mixed a bad Bearer token with an upstream outage: enough
  for an alert threshold, useless for deciding what to do about it. Adds
  `rekai_errors_by_kind_total{kind}` (`unauthorized`, `rate_limited`,
  `concurrency_limit`, `budget_exceeded`, `payload_too_large`,
  `guardrail_blocked`, `idempotency_error`, `provider_error`) and
  `rekai_provider_errors_total{provider,status}`, the latter recorded for every
  upstream failure — including non-transient 4xx and the last attempt in a
  fallback chain, which the old fallback-only call site dropped — so a
  per-provider success rate is computable against
  `rekai_provider_requests_total`. Two paths that returned an error and counted
  *nothing* now do: the hard body-size cap (the one a chunked upload actually
  trips) and `Idempotency-Key` 409/422 conflicts.
- **Latency is measured** — RekAI reported cost, tokens, cache hits, retries and
  cooldowns, but not a single duration: `elapsed_ms` was computed in the request
  middleware for the `X-Response-Time-Ms` header and the access log, then thrown
  away, and upstream calls were never timed at all. Three Prometheus histograms
  on the OpenTelemetry GenAI advisory bucket boundaries for
  `gen_ai.client.operation.duration`: `rekai_request_duration_seconds{path}`
  (end-to-end, labelled by route template so a parameterised route is one
  series), `rekai_provider_duration_seconds{provider,operation}` (upstream,
  retries included), and `rekai_stream_ttft_seconds{provider}` (time to first
  streamed token). The gap between the first two is RekAI's own overhead — the
  thing that was previously unknowable.

### Changed
- **Prompt-injection guardrail: patterns narrowed, and the default action is now
  `flag` instead of `block`** (**breaking** for deployments running with
  `REKAI_GUARDRAILS_ENABLED=true` and no explicit action — they stop returning
  403 and start returning 200 with `X-Guardrail-Flag`; set
  `REKAI_GUARDRAILS_ACTION=block` to keep the old behavior). The patterns matched
  a verb plus a bare noun, so all 17 benign phrasings now in the test corpus were
  flagged — "show me the instructions for assembling this bookshelf", "override
  the system clock in a unit test", "summarize this security paper about
  jailbreak techniques", "disregard the previous draft" — each a hard 403 on
  ordinary traffic. Every pattern now requires an object referring to the model's
  own instructions or safety configuration: 0/17 false positives, 0/21 attack
  phrasings missed (up from 6 attack cases covered). The default changed because
  a regex wrong in the blocking direction deletes a legitimate request with no
  recourse, while one wrong in the flagging direction costs a header — and since
  pattern matching can't be a boundary against an adversary who rephrases
  (arXiv:2504.11168), its realistic value is signal, which doesn't require
  blocking.
- **`rekai_requests_total{provider="…"}` renamed to
  `rekai_provider_requests_total{provider="…"}`** (**breaking** for dashboards
  using the old series). The per-provider breakdown shared a metric name with
  the bare total, so `sum(rekai_requests_total)` counted every request twice and
  Prometheus saw inconsistent label sets within one family.
- **E2E coverage for the v1.2 OpenAI-compatible surface** — added
  `e2e/openai-compat.spec.ts`: a direct `POST /v1/chat/completions` asserting
  the `chat.completion` shape (object, choices, `finish_reason`, usage), a
  `stream: true` run asserting `chat.completion.chunk` frames and a terminal
  `[DONE]` with reassembled deltas, and a check that the Settings page's "Use it
  from the OpenAI SDK" snippet shows the drop-in base URL.
- **Registry test for `REKAI_CUSTOM_*` env wiring** — the env → registry path
  that registers a custom OpenAI-compatible backend at import time was
  untested (existing tests only built the provider directly). Added a
  reload-based test that sets `REKAI_CUSTOM_BASE_URL`/`_NAME`/`_MODELS` and
  asserts the provider is registered with the right URL, key, and models (and
  cleans up so global registry state is restored for later tests).
- **`scripts/smoke.sh` asserts JSON fields with `jq`, not substring greps** —
  the smoke test matched compact-JSON fragments (`"status":"ok"`), which is
  brittle to whitespace/key-order changes and can't check nested values. It now
  uses `jq -e` field assertions (e.g. `.usage.total_tokens > 0`, the echo
  provider present in `.data`) and gained an optional auth negative case: set
  `REKAI_API_KEY` and it verifies an unauthenticated `/v1/chat` returns 401 (and
  authenticates the other checks). Documents the `jq` prerequisite in the
  README and Makefile.
- **Single model registry (`rekai/models.py`)** — model→provider routing, the
  price table, and each provider's advertised `/v1/models` list were maintained
  in three separate places and drifted (o1/o3 and gemini-2.5-pro were priced but
  hidden from `/v1/models`, fixed earlier by hand-syncing all three). They now
  derive from one `ModelSpec` registry: `router` reads its prefix rules,
  `pricing` builds its table from it, and providers advertise from it. A
  `test_models.py` invariant asserts every advertised chat model routes back to
  its provider and is priced, so a model can't be added to one surface and
  forgotten on the others. No behavior change — `/v1/models`, routing, and cost
  estimates are identical.
- **Multi-replica metrics aggregation** — persisted metrics used a single Redis
  key that every replica overwrote (last-writer-wins), so `/v1/usage` reflected
  only whichever process flushed last. Each replica now persists to its own
  `rekai:metrics:snapshot:<instance-id>` key and loads only *its own* snapshot as
  its startup baseline; `/v1/usage` sums this instance's live counters with every
  other replica's persisted snapshot for a fleet-wide view. `/metrics` stays
  per-instance so a Prometheus scraper (which already sums targets) doesn't
  double-count. Instance id comes from `REKAI_INSTANCE_ID` or a random
  per-process id. Added a pure `merge_snapshots()` and `MetricsStore.load_others()`.
- **Idempotency-Key semantics hardened (Stripe-style)** — a key reused with a
  *different* request body now returns **422** instead of silently replaying the
  first (unrelated) response, and a second request that arrives while the first
  is still in flight returns **409** instead of racing it. Each record stores a
  sha256 fingerprint of the request body, and the key is claimed atomically with
  an in-progress sentinel (`cache.add` → Redis `SET NX` / an event-loop-atomic
  memory write) that is released if processing errors so a retry isn't blocked.
  All cache access fails open on backend errors. Applies to both `/v1/chat`
  (and the OpenAI-compatible route) and `/v1/embeddings`. The `CacheBackend`
  protocol gained atomic `add()` and `delete()`.

### Fixed
- **`MemoryCache.add()` can re-claim a just-expired key** — the previous fix
  moved `get()` and `_evict_expired_if_full()` to `expires_at <= now`, but
  `add()` was left on `item[0] >= now`, so at the boundary (an entry written
  with `ttl=0`, or read exactly when it is due) `add()` still saw the entry as
  live and returned `False` while `get()` reported it gone. The in-progress
  idempotency sentinel is claimed through `add()`, so on that path a key could
  never be re-claimed: the caller was refused the claim yet found no stored
  response. `add()` now uses the same strict boundary (`item[0] > now`).
  Regression tests `test_memory_cache_add_reclaims_an_expired_key` (frozen
  clock, no intervening `get()` — a read would evict the entry and mask the
  bug) and `test_memory_cache_add_refuses_a_live_key`.
- **`MemoryCache` expires entries with `ttl=0` correctly** — `get()` and
  `_evict_expired_if_full()` compared `expires_at < now`, so an entry written
  with `ttl=0` (expires immediately) had `expires_at == now` and was treated as
  still valid. Both comparisons now use `<=` so `ttl=0` is expired on the next
  read and is pruned when the cache is at capacity. `tests/test_cache.py`
  (`test_memory_cache_expiry`, `test_memory_cache_evicts_expired_at_capacity`)
  now pass.
- **Provider HTTP client honors a changed request timeout** — `Provider._client()`
  cached its persistent `httpx.AsyncClient` keyed only on the event loop, so a
  changed `request_timeout_seconds` (e.g. re-running `create_app` with new
  settings) was frozen at the value seen when the client was first built. The
  cache key now includes the timeout, so the client is rebuilt when it changes.
- **`/v1/models` now lists every model RekAI routes and prices** — the router
  sends `o1*`/`o3*` to OpenAI and the price table knows `o1`, `o1-mini`,
  `o3-mini`, and `gemini-2.5-pro`, but each provider's `list_models()` omitted
  them, so `/v1/models` hid models that RekAI actually handles (and their
  cost estimates). Added them, and a `test_providers.py` invariant that every
  advertised chat model both has a price and routes back to the advertising
  provider — locking the three surfaces (router, price table, model list)
  together so they can't drift apart again.
- **Admin page detects "admin API not configured" by status code, not error
  text** — the page treated a `404` as "this deployment has no admin API"
  (the routes aren't mounted unless `REKAI_ADMIN_KEY` is set) but did so by
  matching FastAPI's `"Not Found"` message string, which silently breaks if
  that server-controlled text ever changes. `errorFromResponse` now returns a
  typed `ApiError` carrying `.status`, and the page branches on
  `e.status === 404`.
- **Chat UI accessibility** — the conversation is now a labelled `role="log"`
  `aria-live="polite"` region so assistive tech announces streamed and appended
  replies, the error banner is `role="alert"`, and message bubbles carry stable
  ids instead of array-index React keys (which shifted on
  regenerate/clear and confused reconciliation). Added an E2E assertion for the
  live region.

### Added
- **Provider prompt-cache passthrough and discounted cost accounting** — RekAI
  dropped `cache_control` on the way to the provider, so callers couldn't use
  Anthropic's prompt caching (up to ~90% off a cached prefix) through the
  gateway, and cost estimates ignored caching entirely. `ChatRequest` and
  `ChatMessage` now accept a `cache_control` breakpoint: a top-level one marks
  the last prompt block, a per-message one marks that message (a plain string
  body is promoted to a text block so the marker has somewhere to live).
  `Usage` gained `cache_read_tokens` / `cache_write_tokens` — a *breakdown* of
  `prompt_tokens`, defaulting to 0 so existing responses are unchanged —
  populated from Anthropic's `cache_read_input_tokens` /
  `cache_creation_input_tokens` (non-streaming and streaming) and OpenAI's
  `prompt_tokens_details.cached_tokens`. `estimate_cost` bills cache reads at
  0.1x and writes at 1.25x the input rate, with the remainder at full price, so
  a cached prompt is never double-counted. `cache_key` includes the breakpoint
  so differently-cached requests don't share an entry.
- **First-class streaming tool calls in both SDKs** — when a streamed
  completion ends with tool calls, they ride on the summary event under
  `tool_calls`. The SDKs now expose an `on_tool_calls` (`onToolCalls`) stream
  callback invoked with just that list, so callers no longer have to dig them
  out of the usage summary. The server's SSE frame shape is unchanged
  (backward-compatible); `on_usage` still receives the full summary.
- **SDK idempotency keys + client-side retry (both SDKs)** — the Python and JS
  clients now retry transient failures (connection errors and
  `429`/`502`/`503`/`504`) with exponential backoff, honoring `Retry-After`;
  tunable via `max_retries`/`retry_backoff` (`maxRetries`/`retryBackoff` in JS),
  set retries to 0 to disable. `chat()` accepts an `idempotency_key`
  (`idempotencyKey`) sent as the `Idempotency-Key` header so the server replays
  the first response instead of re-processing; when retries are enabled and no
  key is given, one is generated per call (and reused across a request's own
  retries) so an auto-retry can't double-execute. `AsyncRekAIClient` shares the
  same options.
- **`AsyncRekAIClient` in the Python SDK** — an `async`/`await` mirror of the
  synchronous `RekAIClient`, backed by `httpx.AsyncClient` so the connection
  pool is reused across awaits. `stream()` is an async generator driven with
  `async for`, and its `on_usage` callback accepts a coroutine function.
  Shared request/response plumbing (header/payload builders, SSE decoding) was
  factored to module level so the two clients can't drift. Bumps the SDK
  package `__version__` to 1.2.0 (it lagged at 1.1.0 behind `pyproject.toml`).
- **AI-agent working docs** — a root `CLAUDE.md` (shared conventions: the
  exact verification gates, established code idioms, and this environment's
  git constraints) plus `docs/ai/instructions-opus.md` and
  `docs/ai/instructions-sonnet.md`: a strengths/weaknesses summary of the
  codebase and a prioritized improvement backlog split by task difficulty —
  architecture-level work (settings DI for the provider layer, semantic-cache
  confidence bands, cost-quality cascade routing) for Opus-class sessions,
  pattern-following implementation (E2E coverage for v1.2 features, custom-
  provider registry tests, npm audit cleanup, post-permission-grant release
  steps) for Sonnet-class sessions. Every command in the docs was executed
  as written before committing. A second audit pass deepened the backlog
  with 10 more findings (body-unbound Idempotency-Key and in-flight
  coalescing, multi-replica metrics last-writer-wins, stale price/model
  tables, insecure-by-default CORS, admin-page 404 string-matching, chat-UI
  accessibility, async Python SDK, SDK idempotency/retry, streamed tool-call
  surfacing, brittle smoke.sh) plus two strengths to guard against
  regression (dependency-free W3C tracing, bounded Retry-After waits), all
  cited to file:line.

## [1.2.0] - 2026-07-17

### Performance
- **Providers reuse a persistent `httpx.AsyncClient`** — every provider
  (OpenAI, Anthropic, Gemini, Ollama, and OpenAI-compatible) opened a brand-new
  `httpx.AsyncClient` in an `async with` block and tore it down on *every*
  chat/streaming/embeddings request, so no TCP/TLS connection to an upstream
  provider was ever reused — a full handshake per call. A new
  `Provider._client()` helper (in `providers/base.py`) returns a client cached
  per provider instance, keyed by the running event loop (a client's pool is
  loop-bound, so it's rebuilt only when the loop changes — routine only under
  pytest-asyncio, a no-op in production's single long-lived loop). Meaningful
  latency/throughput win under load; no behavior change.

### Added
- **`tests/test_service.py`** — direct unit tests for
  `service.handle_chat_stream`, the shared streaming pipeline extracted in an
  earlier commit. It previously had no dedicated test file, only indirect
  coverage via the streaming endpoints' SSE-serialized output; these drive it
  directly and assert on the typed `ChatStreamEvent`/`StreamSummary` values
  (usage estimation vs provider-reported, tool-call surfacing, 429/5xx
  cooldown and circuit-breaker behavior including reset-on-success, and
  per-client budget-window recording).

### Fixed
- **Dynamic-key decryption failure now logs a warning instead of failing
  silently** — a wrong or rotated `REKAI_DYNAMIC_KEYS_ENCRYPTION_KEY` made
  `DynamicKeyStore.list_keys()` quietly return an empty list, which looks
  identical to "all runtime-added keys were revoked" from an operator's
  side, with nothing in the logs to explain it. Now logs at warning with the
  likely cause, and calls out that the next `add()` (which reads the
  existing list first) would otherwise silently overwrite the undecryptable
  blob.
- **Ollama silently dropped `tools` and `response_format`** — Ollama's
  `/api/chat` has no equivalent for either, and unlike Anthropic's
  `response_format` handling (added earlier), nothing logged that they were
  being ignored. Now logs at debug, matching the Anthropic precedent. Also
  added `tests/test_ollama.py` — previously the provider had only incidental
  coverage via a fake-client embeddings test and generic router/streaming
  tests using the `echo` provider.
- **Documented that `Idempotency-Key` doesn't cover streaming** — neither
  `POST /v1/chat/stream` nor `stream: true` on the OpenAI-compatible
  endpoint accept it (a retried streaming request always re-runs), but this
  was previously undocumented — a new integrator would only discover it by
  reading `main.py`. Now called out in both endpoint docstrings and
  docs/architecture.md's Idempotency section.
- **Request body size limit is now a hard cap, not just a Content-Length
  check** — the previous check only rejected requests that sent an oversized
  `Content-Length` header; a client using chunked transfer-encoding (which
  omits `Content-Length` entirely) sailed straight past it, and Starlette
  would buffer the *entire* body before any size validation ran — no real
  protection against a memory-exhaustion DoS. Added `MaxBodySizeMiddleware`
  (`apps/api/rekai/main.py`), a pure-ASGI middleware wrapping the whole app:
  it buffers `/v1/*` request bodies up to the limit and sends a 413 directly
  the moment the running total is exceeded, without ever invoking the
  downstream app. (A `BaseHTTPMiddleware`-based approach — raising mid-read
  from inside the existing `_rate_limit` middleware — turned out to be
  fundamentally broken: Starlette's internal receive-forwarding wraps such an
  exception in an `anyio.ExceptionGroup`, which loses its type before
  FastAPI's body-parsing code can recognize it as an `HTTPException`, so it
  fell through to a generic 400. The pure-ASGI buffer-then-replay approach
  sidesteps that translation entirely.) The existing Content-Length check
  remains as a cheap, no-buffering fast-path rejection.
- **Per-client tracking is now bounded** (`REKAI_MAX_TRACKED_CLIENTS`, default
  10,000; `0` = unlimited) — `usage_by_client` and the budget-window store grew
  one entry per distinct client id forever, and without gateway auth that id is
  the raw request IP, making any internet-facing deployment a slow memory leak
  (persisted across restarts via the metrics snapshot, no less). The rate
  limiter already capped its buckets for exactly this reason; the metrics
  structures now do too. At the cap, admitting a new client evicts the
  least-active tracked client (the budget-window store first clears entries
  from already-expired windows); `seed()` applies the cap to oversized
  persisted snapshots, keeping the busiest clients. Eviction resets that
  client's lifetime-budget baseline — consistent with budget enforcement being
  documented as approximate, not billing (see docs/architecture.md).

### Added
- **`response_format` in both SDKs** — the Python client's `chat()`/`stream()`
  take `response_format={...}` and the JS client takes
  `responseFormat: {...}`, forwarded to the gateway's `response_format`
  passthrough added below, so SDK users get structured outputs without
  hand-building requests.
- **OpenTelemetry GenAI attributes in the access log** — chat and embeddings
  requests now attach the OTel GenAI semantic-convention fields
  (`gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`,
  `gen_ai.usage.input_tokens`/`output_tokens`) to the structured access-log
  line, so RekAI's JSON logs drop straight into a GenAI observability dashboard
  (Datadog, Grafana, …) without a full OTel SDK integration. Streaming requests
  carry the model/provider fields (set pre-stream) but not token usage (the log
  line fires before the stream body is consumed).
- **`response_format` passthrough (structured outputs)** — both `/v1/chat` and
  the OpenAI-compatible endpoint now accept OpenAI's `response_format`
  (`{"type": "json_object"}` or `{"type": "json_schema", ...}`) and forward it
  to providers that support it: OpenAI and OpenAI-compatible backends natively,
  Gemini best-effort (mapped to `responseMimeType`/`responseSchema`). Anthropic
  and Ollama have no equivalent and ignore it (logged at debug, never an error).
  The 2026 production default for reliable JSON output, which RekAI didn't pass
  through at all. Note: `response_format` is now part of the chat cache key (a
  JSON-mode and a plain request must not collide), so existing cache entries are
  invalidated once on deploy — TTL-bounded and harmless.
- **OpenAI-compatible `POST /v1/chat/completions`** — point any OpenAI SDK,
  LangChain, or other OpenAI-format client at RekAI's base URL (`.../v1`) and it
  works as a drop-in: same request/response shapes, non-streaming and
  `stream: true` (SSE `chat.completion.chunk` frames, with
  `stream_options.include_usage` honored). It's a thin translation
  (`rekai/openai_compat.py`) over the same internal pipeline as `/v1/chat`, so
  routing, cache, retries, fallback, budgets, per-client accounting, and
  Idempotency-Key all apply unchanged. RekAI extensions: an optional `provider`
  field or an OpenRouter-style `"<provider>/<model>"` model string forces a
  provider; unknown OpenAI tuning params (`seed`, `frequency_penalty`, …) are
  tolerated and ignored; `n > 1` is a 400. Errors use the OpenAI error envelope
  so SDK error handling parses them. This is the 2026 de-facto gateway
  interface (LiteLLM/OpenRouter/vLLM/Ollama all expose it); RekAI previously
  had only its own custom `/v1/chat` schema. Verified end-to-end against the
  real `openai` Python SDK (`tests/test_openai_sdk_e2e.py`, ASGITransport).

### Changed
- **Streaming chat pipeline extracted to `service.handle_chat_stream`** — the
  provider-driving loop (deltas, cooldown/circuit-breaker on error, usage
  estimation, per-client accounting) previously lived inline in the
  `/v1/chat/stream` route and hardcoded the SSE frame format. It now yields
  typed `ChatStreamEvent`s and the route formats them, so a second transport
  (the upcoming OpenAI-compatible endpoint) can reuse the exact same pipeline.
  The non-streaming path's guardrail/idempotency/redaction/accounting wrapper
  was likewise factored into `_run_chat`. Byte-for-byte identical output on
  `/v1/chat` and `/v1/chat/stream`; pure refactor, no behavior change.

### Added
- **Gateway-key support in `examples/`** — `curl.sh` and all 7 Python/JS
  example scripts only ever sent the BYOK provider key; none could reach a
  gateway with `REKAI_API_KEYS` configured. All now read a `REKAI_GATEWAY_KEY`
  env var and send it as `Authorization: Bearer`, matching the SDK fix above.
- **Documented `REKAI_APP_NAME` and `REKAI_REQUEST_TIMEOUT_SECONDS` in
  `.env.example`** — both are real, active `Settings` fields (the latter
  controls every outbound HTTP timeout to a provider — chat, streaming,
  embeddings) that had no entry in the example file, found via a
  config-fields-vs-`.env.example` diff. No behavior change.
- **Gateway-key support in both SDKs** — the Python and JS clients only ever
  sent the BYOK provider key (`X-Provider-Key`); neither could authenticate to
  a gateway with `REKAI_API_KEYS` configured, since that needs a separate
  `Authorization: Bearer` header. Both now accept a `gateway_key`/`gatewayKey`
  (constructor default, overridable per call) and send it on every `/v1/*`
  call (`chat`, `stream`, `embeddings`, `models`, `usage`).
- **`X-Content-Type-Options: nosniff` on all API responses** — set in the
  existing `_request_context` middleware alongside `X-Request-ID` /
  `X-RekAI-Version`, closing the gap where the web app got security headers
  but the API itself didn't.
- **Favicon** — `apps/web/app/icon.svg`; the app previously shipped no icon at
  all, so browsers requested a nonexistent `/favicon.ico`. Uses Next.js's
  built-in `app/icon.svg` convention (auto-linked in `<head>`, no other
  wiring needed).
- **Custom error and 404 pages** — `apps/web/app/error.tsx` (client-side error
  boundary with a "Try again" reset button) and `apps/web/app/not-found.tsx`
  (styled 404 matching the rest of the app) replace Next.js's generic
  defaults.
- **Web security headers** — `apps/web/next.config.js` now sets
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: strict-origin-when-cross-origin`, and a `Content-Security-Policy`
  (`connect-src` includes `NEXT_PUBLIC_API_URL` so API calls aren't blocked).
  Verified live: all 5 pages load and a real chat round-trip completes with
  zero CSP violations in the browser console.
- **Non-root Docker users** — both Dockerfiles now drop root before running
  the server (`rekai` for the API image, the built-in `node` user for the
  web image); the web image's `deps` stage also switched from `npm install`
  to `npm ci` for reproducible builds. Reviewed for correctness (standard,
  well-documented patterns) but not build-verified in this session — the
  sandbox's Docker daemon isn't available (`dockerd` fails to start here);
  `docker compose config` validates cleanly.
- **Web E2E suite** — `apps/web/e2e/` (Playwright) promotes three flows
  hand-verified ad-hoc throughout development into committed regression
  tests: sending a chat message, gateway auth locking out the app until a
  key is saved in Settings, and the `/admin` runtime key-management UI
  (wrong key errors, add/revoke). Each spec starts/stops its own API process
  with the `REKAI_*` env it needs (fixed ports, so specs run serially — see
  `apps/web/e2e/README.md`). `npm run e2e`.
- **Time-boxed per-client budget windows** — `REKAI_CLIENT_BUDGET_WINDOW_SECONDS`
  turns `REKAI_CLIENT_BUDGET_USD` from a lifetime-until-reset cap into a fixed
  window (e.g. `86400` for daily, `2592000` for 30 days), using the same
  epoch-aligned `int(now / window)` bucketing `RedisRateLimiter` uses. Unset
  (default) = today's lifetime-cumulative behavior, unchanged. The 402
  response gets a new `X-Budget-Reset` header (next window boundary) when a
  window is configured. Tracked separately from `usage_by_client` and not
  persisted across restarts — see docs/architecture.md. Closes the last
  open item from this session's feature-triage list.
- **`/admin/*` rate limiting** — on by default whenever `REKAI_ADMIN_KEY` is
  set (`REKAI_ADMIN_RATE_LIMIT_*`, 20 requests/60s), sized and keyed
  separately from the tenant rate limit (by IP, since there's no per-admin
  identity). Deliberately checked *before* the admin-key check — the opposite
  order from the tenant gateway-auth gate — so a wrong-key guess still
  consumes budget: the threat here is brute-forcing the one shared secret, not
  fairness between tenants. A firewall/VPN in front of `/admin/*` remains the
  primary control; this is a backstop.
- **Web UI for dynamic key management** — `/admin` in the web app wraps the
  API's `/admin/keys` (add/revoke/list runtime keys, `REKAI_DYNAMIC_KEYS_ENABLED`)
  in a page instead of curl-only access, with its own admin-key field
  (local storage, distinct from the gateway/provider keys) and a clear notice
  when `REKAI_ADMIN_KEY` isn't configured server-side.
- **`traceparent` forwarded upstream** — RekAI parsed and returned W3C Trace
  Context, but the trace stopped at its edge: the outbound HTTP call to the
  actual provider (OpenAI/Anthropic/Gemini/Ollama/custom; chat, streaming, and
  embeddings) never carried a `traceparent`, so a distributed trace couldn't
  follow the request into whichever provider served it. Now every provider
  call attaches a `traceparent` continuing the request's trace with a fresh
  span id, via an ambient `ContextVar` set by the request middleware — no
  `trace_id` parameter threading required down through the call stack.
- **Admin API audit log** — every `/admin/keys` request (add, revoke, list,
  and unauthorized attempts) is now written to a dedicated `rekai.admin`
  logger with the action, masked key, and caller IP — the key issuance/
  revocation operations previously left no record beyond the generic access
  log line. The raw key is never logged.
- **`REKAI_PRICING_OVERRIDES`** — override or extend the built-in per-model
  price table without a code change or redeploy, e.g.
  `"gpt-4o:2.00:8.00,my-model:0.50:1.50"`. Config-driven (scoped to one
  `Settings` instance, doesn't mutate shared global state like
  `pricing.register_price()` does) and read by every cost estimate, budget
  cap, and the `pricing` field in `/v1/models` — closing the gap where a
  stale/wrong price, once shipped, needed a code change to fix.
- **Circuit breaker for repeated 5xx** — a 429 already parked a provider
  immediately (an explicit "back off" signal); a bare 5xx did not, so a
  provider stuck returning 500s paid for a full retry-with-backoff cycle on
  *every* request before falling over to a fallback. `REKAI_CIRCUIT_BREAKER_THRESHOLD`
  (default 3) now parks a provider the same way after that many *consecutive*
  5xx failures across separate requests (any success resets the count) —
  covers the fallback-chain path and the streaming path.
- **Redis-shared rate limiting** — with `REKAI_REDIS_URL` set, the per-tenant
  rate limit is enforced with a fixed-window Redis `INCR` counter shared by
  all workers/nodes, instead of each process keeping its own token bucket
  (which silently multiplied the effective limit by the worker count). Same
  headers (`X-RateLimit-*`, `Retry-After`); fails open on a Redis outage so
  rate limiting degrades before availability does. Verified live with two
  uvicorn workers: exactly 5 of 10 requests pass at a limit of 5.
- **Dynamic API key management** (opt-in) — `REKAI_DYNAMIC_KEYS_ENABLED` adds a
  runtime-managed set of keys on top of the static `REKAI_API_KEYS`, so an
  operator can onboard or cut off a tenant without a redeploy: `GET/POST
  /admin/keys` and `DELETE /admin/keys/{key}`, guarded by a separate
  `REKAI_ADMIN_KEY` (the admin routes aren't registered at all unless it's
  set). Dynamically-added keys work everywhere a static one does — rate
  limiting, per-client usage, budget caps. Stored via the existing cache
  backend (Redis if configured, else process-local).
  `REKAI_DYNAMIC_KEYS_ENCRYPTION_KEY` (a Fernet key) encrypts that storage at
  rest, since unlike BYOK these keys are actually persisted server-side; unset
  (default) stores plaintext as before.
- **`REKAI_METRICS_REQUIRE_AUTH`** (opt-in) — `/metrics` is open by default
  (so Prometheus can scrape without a token) even when `REKAI_API_KEYS` gates
  `/v1/*`, but it now carries a per-client cost breakdown. This flag locks it
  behind the same Bearer key for operators who consider that sensitive; a
  no-op when no keys are configured. `/v1/usage` was already gated (it's under
  `/v1/*`) — this closes the one endpoint that wasn't.
- **Per-client budget cap** (opt-in) — `REKAI_CLIENT_BUDGET_USD` turns the
  per-client cost tracked in `usage_by_client` into an enforceable spend cap:
  once a client's cumulative cost reaches it, further `/v1/*` requests from
  that client get `402 Payment Required` (`X-Budget-Remaining: 0`), checked
  before any provider call is made. Unset (default) = no cap.
  `REKAI_CLIENT_BUDGETS_USD` (e.g. `"sk-a:5.00,sk-b:20.00"`) overrides the cap
  per API key, falling back to the global default for unlisted keys — needed
  for a multi-tenant deployment where different clients need different caps.
- **Per-client usage metrics** — `/v1/usage` and `/metrics` now break down
  requests, tokens, and cost per client (`usage_by_client` /
  `rekai_client_*_total{client="…"}`), keyed by the same masked `key:<hash>` id
  used for per-tenant rate limiting (or the client IP with no gateway auth).
  Covers chat, streaming, and embeddings, including cached/idempotent-replay
  responses. The raw key is never stored or logged. `usage_by_client` round-trips
  through the write-behind metrics store like every other counter — seeded on
  startup, accumulated live, flushed on shutdown — and is covered by a
  dedicated persistence test.
- **Output redaction** (opt-in, OWASP LLM02) —
  `REKAI_OUTPUT_REDACTION_ENABLED=true` scrubs common secret/API-key patterns
  (OpenAI/Anthropic/Stripe/GitHub/Slack keys, AWS access key ids, `Bearer`
  tokens, PEM private-key blocks) from the assistant's reply on non-streamed
  `/v1/chat`, before it's cached or stored for idempotency replay. Sets
  `X-Redacted: <pattern,...>` when it fires. A heuristic (not a security
  boundary); intentionally not applied to `/v1/chat/stream` (see
  `docs/architecture.md`).
- **Redis-shared provider cooldown** — when `REKAI_REDIS_URL` is set, a
  provider's 429 cooldown is written through to and read from Redis (in
  addition to the local, zero-latency check), so a rate limit discovered by one
  worker/node is honoured by the others instead of each rediscovering it
  independently. No Redis configured → unchanged, process-local behaviour.
- **Prompt-injection guardrail** (opt-in, OWASP LLM01) —
  `REKAI_GUARDRAILS_ENABLED=true` scans user messages on `/v1/chat[/stream]` for
  common injection/jailbreak phrasings; `block` (default) rejects with `403`,
  `flag` adds an `X-Guardrail-Flag` header. A heuristic first layer (not a
  security boundary) — see arXiv:2504.11168 on evasion. Off by default.
- **Per-tenant rate limiting** — when gateway auth is on, the rate-limit bucket
  is keyed by the API key (a non-reversible `key:<hash>` id) instead of the
  client IP, so one tenant can't exhaust another's budget; the masked id is also
  attached to the structured access log as `client`. Falls back to IP when
  unauthenticated.
- **Gateway authentication** (opt-in) — set `REKAI_API_KEYS` (comma-separated)
  to require `Authorization: Bearer <key>` on `/v1/*`, compared in constant time
  (`401` + `WWW-Authenticate: Bearer` otherwise). Checked before rate limiting so
  unauthenticated traffic can't consume budget; system endpoints (`/health`,
  `/metrics`) stay open. Distinct from BYOK (the upstream provider key). Empty by
  default (open).
- **W3C Trace Context** — RekAI parses an incoming `traceparent`, continues its
  `trace_id` (emitting a new span id) or starts a fresh trace, returns a
  `traceparent` response header, and attaches `trace_id` to the structured
  access log — so it participates in distributed traces in an OpenTelemetry
  system, dependency-free.
- **Resilience metrics** — `rekai_retries_total` (transient failures retried in
  place) and `rekai_cooldowns_total` (providers parked after a 429) are now
  counted in `/metrics` and `/v1/usage`, and shown on the web usage dashboard,
  so operators can see the retry/cooldown machinery working.
- **Semantic cache** (opt-in) — `REKAI_SEMANTIC_CACHE_ENABLED=true` reuses a
  response when a prior prompt's embedding is within
  `REKAI_SEMANTIC_CACHE_THRESHOLD` cosine similarity (default 0.85), catching
  paraphrases that the exact-match cache misses — the *GPT Semantic Cache*
  approach (arXiv:2411.05276). Entries are bucketed by provider/model/params and
  held in a bounded process-local store. Costs one embedding call per request,
  so use a real embeddings model.
- **Idempotency-Key** — clients can send an `Idempotency-Key` header on
  `POST /v1/chat` / `/v1/embeddings`; a repeat with the same key returns the
  stored first response (`Idempotent-Replay: true`) instead of processing again,
  so a network blip or automatic retry can't double-process. Keyed by the
  client id (not the body), works with `"cache": false`, TTL
  `REKAI_IDEMPOTENCY_TTL_SECONDS` (default 24h).
- **Automatic retry with backoff + jitter** — transient upstream failures
  (5xx / network timeouts) are now retried in place before falling over, with
  exponential backoff and full jitter (`REKAI_RETRY_MAX_ATTEMPTS`, default 2;
  `REKAI_RETRY_BASE_DELAY_SECONDS`, `REKAI_RETRY_MAX_DELAY_SECONDS`). 4xx errors
  are never retried. Applies to chat and embeddings. (A resilience pattern
  widely recommended for LLM API clients — retry transient errors with jittered
  exponential backoff rather than failing or hammering a recovering upstream.)
- **Upstream rate-limit (429) handling** — a provider 429 is now retried
  honouring its `Retry-After` (waiting that long when it's within `max_delay`),
  triggers failover to the next target, and — when ultimately surfaced — its
  `Retry-After` is **passed through to the client** so the caller's SDK backs
  off by the amount the provider asked for (previously 429 was terminal and the
  header was dropped). `ProviderError` gained `retry_after`.
- **Provider cooldown** — after a 429 a provider is parked for its `Retry-After`
  (or `REKAI_PROVIDER_COOLDOWN_SECONDS`, default 30s) and routing skips it in
  favour of a healthy fallback while it cools down, so RekAI stops hammering a
  rate-limited provider across requests. Toggle with
  `REKAI_PROVIDER_COOLDOWN_ENABLED`.

### Fixed
- **Password fields wrapped in `<form>`** — the Settings and Admin pages'
  password/credential inputs (provider key, gateway key, admin key, add/revoke
  runtime key) previously used bare `onClick` handlers, which browsers warn
  about ("password field is not contained in a form") and which break
  Enter-to-submit and password-manager integration. Each is now inside its own
  `<form onSubmit>`.
- **Fail-fast config validation** — enum-like settings (`REKAI_LOG_FORMAT`,
  `REKAI_GUARDRAILS_ACTION`) are now `Literal`-typed and numeric ones carry
  bounds (`SEMANTIC_CACHE_THRESHOLD` in [0,1], `RETRY_MAX_ATTEMPTS`/rate limits
  ≥ 1, TTL/body-size ≥ 0), so a typo or out-of-range value raises a clear error
  at startup instead of silently falling back to wrong behaviour.
- **Streaming 429 now records a provider cooldown** — a rate limit seen on the
  streaming path is now parked like on the non-streaming path, so subsequent
  requests route around the rate-limited provider (previously only non-streaming
  429s did this).
- **Streaming crash on tools conversations** — when a provider didn't report
  usage, the streaming endpoint estimated tokens over the request messages and
  crashed mid-stream on a message with `content: null` (valid in a tool
  round-trip). It now treats null content as empty, so the stream completes with
  an estimated usage summary.
- **Cache correctness with tools** — the chat cache key now includes `tools`
  and `tool_choice`. Previously two requests with identical messages but
  different tools collided, so a tool-less reply could be served for a tools
  request (and vice versa).
- **Rate-limiter memory bound** — the per-client bucket map now prunes idle
  (fully-refilled) buckets once it passes a soft cap (`max_buckets`, default
  10k), so a flood of distinct client keys can't grow memory without bound.
- **Memory cache bound** — `MemoryCache` drops expired entries before growing
  past `max_entries` (default 10k), instead of only evicting on read.
- **Web app couldn't use gateway auth at all** — enabling `REKAI_API_KEYS`
  broke the entire web app (chat, embeddings, and the usage dashboard all got
  silent or opaque `401`s), because it only ever sent the upstream BYOK
  provider key (`X-Provider-Key`), never a gateway `Authorization: Bearer`
  token. Settings now has a separate "Gateway API key" field, stored and sent
  alongside the provider key on every `/v1/*` call; the usage page also
  surfaces a clear "set the gateway key" hint on `401`.

### Changed
- Refreshed the README and architecture docs to reflect the 1.1.0 feature set
  (all five providers, tool calling, embeddings, model discovery + pricing,
  rate-limit headers, JSON logging, SDKs).

## [1.1.0] - 2026-06-29

A backward-compatible feature release building on 1.0.0: text embeddings across
all providers, richer model discovery (types, pricing, filtering), rate-limit
observability, structured logging, and hardening.

### Added
- **Per-model pricing in `/v1/models`** — each entry now carries an optional
  `pricing` (`input_per_1m`/`output_per_1m` USD, or `null` when unknown) from
  the pricing table, so clients can show cost estimates without hardcoding
  rates. The web app and JS SDK `ModelInfo` types include it.
- **Request body size limit** — `/v1/*` requests whose `Content-Length` exceeds
  `REKAI_MAX_BODY_BYTES` (default 1 MB; 0 disables) are rejected with
  `413 Payload Too Large` before parsing, protecting the server from oversized
  payloads. The `413` (and existing `429`) responses are now documented in the
  OpenAPI schema for the chat/embeddings/stream endpoints.
- **`X-RekAI-Version` header** — every response advertises the gateway version
  that served it (exposed via CORS), so clients and proxies can see which
  version answered.
- **Root banner endpoint** — `GET /` returns a small JSON service banner
  (name, version, description, links to `/docs` and `/health`) so hitting the
  bare API URL is friendly instead of a 404.
- **Structured JSON logging** — set `REKAI_LOG_FORMAT=json` to emit one JSON
  object per log line (`ts`, `level`, `logger`, `message`, plus any `extra=`
  fields). The access log now carries structured `method`/`path`/`status`/
  `duration_ms`/`request_id` fields, so logs are machine-parseable in
  production. Defaults to the human-readable text format.
- **`/v1/models?type=` filter** — fetch only `chat` or only `embedding` models
  server-side (invalid values are rejected with 422). The web Embeddings page
  uses it directly instead of filtering client-side.
- **Rate-limit budget hint in the web chat** — after a request the composer
  shows a subtle "N / M requests left in the rate-limit window", read from the
  `X-RateLimit-*` headers via a new `parseRateLimit()` and an `onRateLimit`
  callback on the chat fetch helpers.
- **Graceful rate-limit UX in the web chat** — a 429 now shows a clear
  "Rate limited — retry in Ns." message (from `Retry-After`) instead of a
  generic failure. Required two fixes so the browser can actually read the
  response: CORS is now the outermost middleware (so a short-circuit 429 still
  carries CORS headers) and the custom headers (`Retry-After`, `X-RateLimit-*`,
  `X-Request-ID`, `X-Response-Time-Ms`) are exposed via
  `Access-Control-Expose-Headers`. CORS preflight (`OPTIONS`) no longer consumes
  rate-limit budget. The web fetch helpers share one `errorFromResponse()`.
- **Rate-limit headers** — every `/v1/*` response now carries
  `X-RateLimit-Limit` and `X-RateLimit-Remaining`, and rate-limited responses
  add a standard `Retry-After` (whole seconds until a token frees up, also
  echoed in the detail). `RateLimiter` gained non-consuming `remaining()` and
  `retry_after()` peeks.
- **Container healthchecks & readiness gating** — the web image gained a
  `HEALTHCHECK` (the API already had one), and Docker Compose now starts `web`
  only once `api` is `service_healthy` (which itself waits on Redis). A
  `docker compose up` comes up in dependency order and reports real readiness.
- **Embeddings** — `POST /v1/embeddings` with provider routing, caching, BYOK,
  and metrics. Echo returns deterministic vectors (no key); OpenAI(-compatible)
  calls the real `/embeddings` API. `Provider.embed()` is the extension point.
  Both SDKs expose `embeddings()` (Python `EmbeddingsResult`, JS returns the
  parsed object) for client parity with the chat path. **Ollama** embeddings
  are native via `/api/embed` (keyless, e.g. `nomic-embed-text`) and **Gemini**
  via `:batchEmbedContents` (e.g. `text-embedding-004`) — vectors now span all
  cloud providers like chat. Embeddings responses carry `cost_usd` (input-only
  pricing for `text-embedding-3-*`/`ada-002`; both SDKs surface it). A web
  **Embeddings** playground (`/embeddings`) embeds one-input-per-line and shows
  vector dims, cost, and pairwise cosine similarity. `/v1/models` now tags each
  entry with a `type` (`chat`/`embedding`) and advertises embedding models
  (`list_embedding_models()`), so the playground offers a real model dropdown
  routed to the right provider and the chat selector stays chat-only.
  OpenAI-compatible backends can advertise their own embedding models via
  `REKAI_CUSTOM_EMBEDDING_MODELS`. Runnable
  `examples/{python,javascript}/embeddings.{py,mjs}` show a cosine-similarity
  demo, and `examples/python/semantic_search.py` ranks a corpus against a query
  (the core of RAG retrieval).
- **Tool / function calling** — `ChatRequest` accepts OpenAI-style `tools` and
  `tool_choice` (passed through); the model's `tool_calls` are returned on
  `ChatResponse`. Messages support `tool_calls`/`tool_call_id`/`name` and
  optional `content` for full round-trips. (Non-streaming; OpenAI-compatible.)
  Both SDKs expose `tools`/`tool_choice` and surface `tool_calls`. For
  streaming, OpenAI tool-call deltas are accumulated and returned in the final
  summary event. **Anthropic** tools work natively via format translation
  (OpenAI `tools`/`tool_choice`/`tool_calls` ↔ Anthropic
  `input_schema`/`tool_use`/`tool_result`). **Gemini** likewise via
  `functionDeclarations`/`functionCall`/`functionResponse` — uniform tool
  calling across OpenAI, Anthropic, and Gemini through one API, in both
  non-streaming and streaming modes. A `examples/python/tools.py` demonstrates a
  full call → execute → respond round-trip.
- **Exact provider routing from the web** — the chat UI sends the selected
  model's provider (from `/v1/models`), so custom and explicitly-chosen
  providers route correctly instead of falling back to the default.
- **OpenAI-compatible provider** — set `REKAI_CUSTOM_BASE_URL` to front any
  OpenAI-compatible API (Groq, Together, OpenRouter, Mistral, vLLM, LM Studio…);
  reuses the OpenAI implementation incl. accurate streaming usage.
- **Deploy configs** — a Render Blueprint (`deploy/render.yaml`) provisioning
  Redis + API + Web, plus a deploy guide. The web `Dockerfile` accepts
  `NEXT_PUBLIC_API_URL` as a build arg so the API URL is baked correctly.
- **Regenerate** — re-run the last user turn for a fresh assistant reply,
  without duplicating messages.
- **Max tokens control** — the chat Options panel now exposes a `max_tokens`
  cap (forwarded on both the streaming and non-streaming requests).
- **Streaming usage/cost** — `POST /v1/chat/stream` now emits a final
  `{"usage", "cost_usd", "estimated"}` summary event, and streamed requests are
  counted in `/v1/usage` and `/metrics` (previously only non-streamed were). The
  web chat shows token/cost on streamed replies.
- **SDK streaming usage** — the Python (`on_usage`) and JS (`onUsage`) clients
  now surface the final streaming usage/cost summary via an optional callback.
- **Accurate streaming usage** — providers gained `stream_events()`; all five
  (echo, OpenAI via `stream_options`, Anthropic, Gemini, Ollama) report exact
  token counts during streaming (`estimated: false`), with text estimation as
  the fallback.

## [1.0.0] - 2026-06-29

First public release — a self-hostable AI router & gateway. Runs with a single
`docker compose up`, works out of the box via the keyless `echo` provider, and
exposes one OpenAI-style chat API across five backends with caching, BYOK,
streaming, fallback, cost estimation, and a built-in web UI.

### Added
- **Monorepo foundation** — `apps/api` (FastAPI) and `apps/web` (Next.js),
  Docker Compose, devcontainer, issue/PR templates, and OSS docs
  (README, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY).
- **Router** — provider resolution by explicit choice → model-name prefix →
  configured default.
- **Provider abstraction** with a registry and five backends: `echo` (keyless),
  `openai`, `anthropic`, `gemini`, and `ollama`.
- **Response cache** — Redis with an automatic in-memory fallback and a
  per-request opt-out; deterministic cache keys.
- **BYOK** — per-request provider keys via `X-Provider-Key`, never persisted.
- **Streaming** — `POST /v1/chat/stream` (SSE) with native token streaming for
  echo, OpenAI, Anthropic, Gemini, and Ollama; safe single-chunk fallback.
- **Cost estimation** — per-model price table, `cost_usd` on responses, and a
  `/v1/usage` summary; cumulative cost in `/metrics`.
- **Fallback / failover** — ordered `(provider, model)` chain retried on
  upstream (5xx) errors; 4xx client errors are terminal.
- **Rate limiting** — per-client token bucket on `/v1/*`.
- **Observability** — structured logging, per-request `X-Request-ID` +
  `X-Response-Time-Ms` headers with access logging, Prometheus-style
  `/metrics`, `/v1/usage`, and auto-generated OpenAPI at `/docs`.
- **Persistent metrics** — write-behind persistence of the usage counters to
  Redis (when configured) so `/v1/usage` totals survive restarts; in-memory and
  no-op otherwise.
- **Web UI** — chat with model selector, streaming toggle, an **Options** panel
  (system prompt + temperature), a **Stop** button to cancel a stream,
  conversation persistence across reloads, and cache/provider/token/cost
  indicators; a live **usage dashboard** at `/usage`; a settings page for BYOK
  keys.
- **Examples** — runnable curl, Python (incl. streaming), and JavaScript
  clients.
- **Python SDK** — installable `rekai-client` package (`packages/python-sdk`)
  with `RekAIClient` (`chat`, `stream`, `models`, `usage`, `health`), BYOK,
  and fallback support.
- **JavaScript/TypeScript SDK** — zero-dependency `@rekai/client`
  (`packages/js-sdk`) mirroring the Python client, with TypeScript types and an
  async-generator `stream()`.
- **CI** — GitHub Actions for API (ruff, mypy, pytest), web (lint, vitest,
  build), Python/JS SDK tests, a live-API smoke job, and Docker image builds.
- **Web unit tests** — vitest coverage for the pure client helpers
  (`formatCost`, `parseSSEFrame`).
- **Makefile** — common developer tasks (`make help`).
- **Smoke test** — `scripts/smoke.sh` exercises the core endpoints of a running
  instance (health, chat, stream, usage, models, OpenAPI).
- **pre-commit** — config running ruff (lint + format) and file-hygiene hooks.
- **.dockerignore** for both apps so image builds exclude local
  `node_modules`/`.venv`/caches.
- **Provider readiness** in `/health` (`provider_status`): `ready` vs
  `byok_only` per provider, surfaced as badges on the web Settings page and as
  an inline chat hint when the selected model needs a key that isn't set.

### Fixed
- Web `output: standalone` is now gated behind `NEXT_OUTPUT=standalone` (set by
  the Dockerfile) so local `next start` works and the app hydrates correctly.
- SDK CI now runs `ruff format --check` (previously only `ruff check`), and the
  SDK source was reformatted to match.

[Unreleased]: https://github.com/shizukutanaka/RekAI/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/shizukutanaka/RekAI/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/shizukutanaka/RekAI/releases/tag/v1.0.0
