#!/usr/bin/env bash
# Smoke-test a running RekAI API. Exercises the core endpoints against the
# keyless `echo` provider, so it needs no credentials by default.
#
# Requires `jq` (checks are field-level, not substring greps).
#
# Usage:
#   scripts/smoke.sh [BASE_URL]
#   BASE_URL=http://localhost:8000 scripts/smoke.sh
#
# If the deployment has gateway auth configured (REKAI_API_KEYS), pass a valid
# key as REKAI_API_KEY to authenticate the /v1/* checks and additionally verify
# that an *unauthenticated* request is rejected with 401:
#   REKAI_API_KEY=sk-rekai-... scripts/smoke.sh
set -euo pipefail

BASE_URL="${1:-${BASE_URL:-http://localhost:8000}}"
GATEWAY_KEY="${REKAI_API_KEY:-}"
pass=0
fail=0

if ! command -v jq >/dev/null 2>&1; then
  echo "error: jq is required (https://jqlang.github.io/jq/). Install it and re-run." >&2
  exit 2
fi

# Auth header interpolated into the inner `bash -c` command strings (empty when
# no gateway key is set, so a keyless echo deployment still passes as-is).
AUTH_ARG=""
if [ -n "$GATEWAY_KEY" ]; then
  AUTH_ARG="-H \"Authorization: Bearer $GATEWAY_KEY\""
fi

check() {
  local name="$1"
  shift
  if "$@"; then
    echo "  ✓ $name"
    pass=$((pass + 1))
  else
    echo "  ✗ $name"
    fail=$((fail + 1))
  fi
}

CHAT_BODY='{"model":"echo","messages":[{"role":"user","content":"ping"}]}'

echo "Smoke-testing RekAI at $BASE_URL"

check "root banner is served" bash -c \
  "curl -fsS '$BASE_URL/' | jq -e '.docs == \"/docs\"' >/dev/null"

check "health is ok" bash -c \
  "curl -fsS '$BASE_URL/health' | jq -e '.status == \"ok\"' >/dev/null"

check "chat (echo) returns content" bash -c \
  "curl -fsS '$BASE_URL/v1/chat' $AUTH_ARG -H 'Content-Type: application/json' \
     -d '$CHAT_BODY' \
   | jq -e '.provider == \"echo\" and (.content | startswith(\"Echo:\")) \
            and (.usage.total_tokens > 0)' >/dev/null"

check "stream emits [DONE]" bash -c \
  "curl -fsS -N '$BASE_URL/v1/chat/stream' $AUTH_ARG -H 'Content-Type: application/json' \
     -d '{\"model\":\"echo\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}' \
   | grep -q '\[DONE\]'"

check "embeddings (echo) return vectors" bash -c \
  "curl -fsS '$BASE_URL/v1/embeddings' $AUTH_ARG -H 'Content-Type: application/json' \
     -d '{\"model\":\"echo\",\"input\":\"hello\"}' \
   | jq -e '(.embeddings | length) > 0 and (.embeddings[0] | length) > 0' >/dev/null"

check "usage exposes counters" bash -c \
  "curl -fsS '$BASE_URL/v1/usage' $AUTH_ARG | jq -e 'has(\"requests_total\")' >/dev/null"

check "models lists the echo provider" bash -c \
  "curl -fsS '$BASE_URL/v1/models' $AUTH_ARG \
   | jq -e '[.data[] | select(.id == \"echo\")] | length > 0' >/dev/null"

check "openapi documents /v1/chat" bash -c \
  "curl -fsS '$BASE_URL/openapi.json' | jq -e '.paths | has(\"/v1/chat\")' >/dev/null"

# Negative auth case — only meaningful when the deployment requires a gateway
# key. An unauthenticated /v1/chat must be rejected with 401.
if [ -n "$GATEWAY_KEY" ]; then
  check "unauthenticated /v1/chat is rejected (401)" bash -c \
    "test \"\$(curl -s -o /dev/null -w '%{http_code}' '$BASE_URL/v1/chat' \
       -H 'Content-Type: application/json' -d '$CHAT_BODY')\" = 401"
else
  echo "  · skipping auth negative case (set REKAI_API_KEY to enable)"
fi

echo
echo "Result: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
