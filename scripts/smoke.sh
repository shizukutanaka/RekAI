#!/usr/bin/env bash
# Smoke-test a running RekAI API. Exercises the core endpoints against the
# keyless `echo` provider, so it needs no credentials.
#
# Usage:
#   scripts/smoke.sh [BASE_URL]
#   BASE_URL=http://localhost:8000 scripts/smoke.sh
set -euo pipefail

BASE_URL="${1:-${BASE_URL:-http://localhost:8000}}"
pass=0
fail=0

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

echo "Smoke-testing RekAI at $BASE_URL"

check "root banner is served" bash -c \
  "curl -fsS '$BASE_URL/' | grep -q '\"docs\":\"/docs\"'"

check "health is ok" bash -c \
  "curl -fsS '$BASE_URL/health' | grep -q '\"status\":\"ok\"'"

check "chat (echo) returns content" bash -c \
  "curl -fsS '$BASE_URL/v1/chat' -H 'Content-Type: application/json' \
     -d '{\"model\":\"echo\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}' \
   | grep -q 'Echo: ping'"

check "stream emits [DONE]" bash -c \
  "curl -fsS -N '$BASE_URL/v1/chat/stream' -H 'Content-Type: application/json' \
     -d '{\"model\":\"echo\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}' \
   | grep -q '\[DONE\]'"

check "embeddings (echo) return vectors" bash -c \
  "curl -fsS '$BASE_URL/v1/embeddings' -H 'Content-Type: application/json' \
     -d '{\"model\":\"echo\",\"input\":\"hello\"}' | grep -q 'embeddings'"

check "usage exposes counters" bash -c \
  "curl -fsS '$BASE_URL/v1/usage' | grep -q 'requests_total'"

check "models lists providers" bash -c \
  "curl -fsS '$BASE_URL/v1/models' | grep -q 'echo'"

check "openapi is served" bash -c \
  "curl -fsS '$BASE_URL/openapi.json' | grep -q '/v1/chat'"

echo
echo "Result: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
