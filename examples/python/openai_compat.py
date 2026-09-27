#!/usr/bin/env python3
"""OpenAI-SDK-shaped call against RekAI's drop-in endpoint — stdlib only.

This is the same request/response shape as ``POST /v1/chat/completions`` on
api.openai.com, so the OpenAI SDK, LangChain, or any OpenAI-format client can
be pointed at RekAI's base URL (``.../v1``) unchanged. Here we show the raw
HTTP shape so no SDK install is needed; with the real SDK it's just
``OpenAI(base_url="<rekai>/v1", api_key=<gateway-or-provider-key>)``.

Usage:
    python python/openai_compat.py "your prompt here"

Environment:
    REKAI_API_URL        API base URL (default http://localhost:8000)
    MODEL                model to request (default "echo")
    REKAI_PROVIDER_KEY   optional BYOK key, sent as X-Provider-Key
    REKAI_GATEWAY_KEY    optional gateway key, sent as Authorization: Bearer
                         (only needed if the deployment has REKAI_API_KEYS set;
                         with no gateway auth at all, Authorization doubles as
                         the BYOK provider key — the OpenRouter convention)
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
        {"model": MODEL, "messages": [{"role": "user", "content": prompt}]}
    ).encode()
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("REKAI_PROVIDER_KEY")
    if key:
        headers["X-Provider-Key"] = key
    gateway_key = os.environ.get("REKAI_GATEWAY_KEY")
    if gateway_key:
        headers["Authorization"] = f"Bearer {gateway_key}"

    req = urllib.request.Request(
        f"{API_URL}/v1/chat/completions", data=body, headers=headers
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        # Errors on this path arrive in OpenAI's envelope: {"error": {...}}.
        detail = exc.read().decode()
        raise SystemExit(f"API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach RekAI at {API_URL}: {exc.reason}") from exc


def main() -> None:
    prompt = " ".join(sys.argv[1:]) or "Hello from the OpenAI-compat surface!"
    result = chat(prompt)
    choice = result["choices"][0]
    usage = result.get("usage") or {}
    print(choice["message"]["content"])
    print(
        f"\n[id={result['id']} finish={choice.get('finish_reason')} "
        f"tokens={usage.get('total_tokens')}]"
    )


if __name__ == "__main__":
    main()
