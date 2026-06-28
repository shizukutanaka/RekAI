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

echo "→ POST ${API_URL}/v1/chat  (model=${MODEL})"
curl -sS "${API_URL}/v1/chat" \
  -H 'Content-Type: application/json' \
  "${key_header[@]}" \
  -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello from curl!\"}]}"
echo
