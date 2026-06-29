#!/usr/bin/env python3
"""Stream a RekAI chat completion (SSE) using only the standard library.

Usage:
    python python/stream.py "your prompt here"

Environment:
    REKAI_API_URL        API base URL (default http://localhost:8000)
    MODEL                model to request (default "echo")
    REKAI_PROVIDER_KEY   optional BYOK key, sent as X-Provider-Key
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API_URL = os.environ.get("REKAI_API_URL", "http://localhost:8000")
MODEL = os.environ.get("MODEL", "echo")


def stream(prompt: str) -> None:
    body = json.dumps(
        {"model": MODEL, "messages": [{"role": "user", "content": prompt}]}
    ).encode()
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("REKAI_PROVIDER_KEY")
    if key:
        headers["X-Provider-Key"] = key

    req = urllib.request.Request(f"{API_URL}/v1/chat/stream", data=body, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            for raw in resp:
                line = raw.decode().strip()
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if payload == "[DONE]":
                    break
                event = json.loads(payload)
                if "delta" in event:
                    print(event["delta"], end="", flush=True)
                elif "error" in event:
                    raise SystemExit(f"\nstream error: {event.get('detail', event['error'])}")
        print()
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach RekAI at {API_URL}: {exc}") from exc


if __name__ == "__main__":
    stream(" ".join(sys.argv[1:]) or "Tell me a short story.")
