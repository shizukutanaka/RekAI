#!/usr/bin/env python3
"""Minimal RekAI client using only the Python standard library.

Usage:
    python python/chat.py "your prompt here"

Environment:
    REKAI_API_URL        API base URL (default http://localhost:8000)
    MODEL                model to request (default "echo")
    REKAI_PROVIDER_KEY   optional BYOK key, sent as X-Provider-Key
    REKAI_GATEWAY_KEY    optional gateway key, sent as Authorization: Bearer
                         (only needed if the deployment has REKAI_API_KEYS set)
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

    req = urllib.request.Request(f"{API_URL}/v1/chat", data=body, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()
        raise SystemExit(f"API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach RekAI at {API_URL}: {exc.reason}") from exc


def main() -> None:
    prompt = " ".join(sys.argv[1:]) or "Hello from Python!"
    result = chat(prompt)
    print(result["content"])
    print(
        f"\n[provider={result['provider']} model={result['model']} "
        f"cached={result['cached']} tokens={result['usage']['total_tokens']}]"
    )


if __name__ == "__main__":
    main()
