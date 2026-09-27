#!/usr/bin/env python3
"""Anthropic-SDK-shaped call against RekAI's drop-in endpoint — stdlib only.

Same request/response shape as ``POST /v1/messages`` on api.anthropic.com, so
the Anthropic SDK can be pointed at RekAI unchanged:
``Anthropic(base_url="<rekai>", api_key=<gateway-or-provider-key>)``. Routing,
cache, retries, fallback, budgets, and metrics all apply — it's the same
pipeline as ``/v1/chat``.

Usage:
    python python/anthropic_compat.py "your prompt here"

Environment:
    REKAI_API_URL        API base URL (default http://localhost:8000)
    MODEL                model to request (default "echo")
    REKAI_PROVIDER_KEY   optional BYOK key, sent as X-Provider-Key
    REKAI_GATEWAY_KEY    optional gateway key, sent as Authorization: Bearer or
                         x-api-key (only needed if the deployment has
                         REKAI_API_KEYS set)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API_URL = os.environ.get("REKAI_API_URL", "http://localhost:8000")
MODEL = os.environ.get("MODEL", "echo")


def chat(prompt: str) -> dict:
    body = json.dumps(
        {
            "model": MODEL,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    key = os.environ.get("REKAI_PROVIDER_KEY")
    if key:
        headers["X-Provider-Key"] = key
    gateway_key = os.environ.get("REKAI_GATEWAY_KEY")
    if gateway_key:
        # Anthropic clients send x-api-key; Bearer works too.
        headers["x-api-key"] = gateway_key

    req = urllib.request.Request(f"{API_URL}/v1/messages", data=body, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        # Errors on this path arrive in Anthropic's envelope:
        # {"type": "error", "error": {"type": ..., "message": ...}}.
        detail = exc.read().decode()
        raise SystemExit(f"API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach RekAI at {API_URL}: {exc.reason}") from exc


def main() -> None:
    prompt = " ".join(sys.argv[1:]) or "Hello from the Anthropic-compat surface!"
    result = chat(prompt)
    text = "".join(
        block.get("text", "")
        for block in result.get("content", [])
        if block.get("type") == "text"
    )
    print(text)
    usage = result.get("usage") or {}
    print(
        f"\n[id={result['id']} stop={result.get('stop_reason')} "
        f"in={usage.get('input_tokens')} out={usage.get('output_tokens')}]"
    )


if __name__ == "__main__":
    main()
