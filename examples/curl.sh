#!/usr/bin/env bash
# Minimal RekAI chat example using curl.
set -euo pipefail

API_URL="${REKAI_API_URL:-http://localhost:8000}"
MODEL="${MODEL:-echo}"

# Build optional BYOK header.
key_header=()
if [[ -n "${REKAI_PROVIDER_KEY:-}" ]]; then
  key_header=(-H "X-Provider-Key: ${REKAI_PROVIDER_KEY}")
fi

# Build optional gateway auth header (only needed if the deployment has
# REKAI_API_KEYS configured — distinct from the BYOK provider key above).
auth_header=()
if [[ -n "${REKAI_GATEWAY_KEY:-}" ]]; then
  auth_header=(-H "Authorization: Bearer ${REKAI_GATEWAY_KEY}")
fi

echo "→ POST ${API_URL}/v1/chat  (model=${MODEL})"
curl -sS "${API_URL}/v1/chat" \
  -H 'Content-Type: application/json' \
  "${key_header[@]}" \
  "${auth_header[@]}" \
  -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello from curl!\"}]}"
echo
