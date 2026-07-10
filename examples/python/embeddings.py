#!/usr/bin/env python3
"""Create text embeddings through RekAI, using only the stdlib.

The keyless `echo` provider returns deterministic vectors, so this runs out of
the box:

    REKAI_API_URL=http://localhost:8000 python python/embeddings.py

For real vectors point at an embeddings model + provider key, e.g.

    MODEL=text-embedding-3-small REKAI_PROVIDER_KEY=sk-... \
    python python/embeddings.py

(Ollama works keyless too: MODEL=nomic-embed-text with Ollama running.)
"""

from __future__ import annotations

import json
import math
import os
import urllib.request

API_URL = os.environ.get("REKAI_API_URL", "http://localhost:8000")
MODEL = os.environ.get("MODEL", "echo")


def embed(inputs: list[str]) -> dict:
    body = {"model": MODEL, "input": inputs}
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("REKAI_PROVIDER_KEY")
    if key:
        headers["X-Provider-Key"] = key
    gateway_key = os.environ.get("REKAI_GATEWAY_KEY")
    if gateway_key:
        headers["Authorization"] = f"Bearer {gateway_key}"
    req = urllib.request.Request(
        f"{API_URL}/v1/embeddings", data=json.dumps(body).encode(), headers=headers
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def main() -> None:
    texts = ["a cat sat on the mat", "a feline rested on the rug", "quarterly revenue grew"]
    result = embed(texts)
    vectors = result["embeddings"]
    print(f"provider={result['provider']} model={result['model']} dim={len(vectors[0])}")

    # Compare the first text against the others by cosine similarity.
    for other, text in zip(vectors[1:], texts[1:]):
        print(f"  sim({texts[0]!r}, {text!r}) = {cosine(vectors[0], other):.4f}")


if __name__ == "__main__":
    main()
