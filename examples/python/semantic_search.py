#!/usr/bin/env python3
"""Tiny semantic search over a corpus using RekAI embeddings (stdlib only).

Embeds a small set of documents once, then ranks them against a query by cosine
similarity — the core of retrieval-augmented generation (RAG). Runs out of the
box on the keyless `echo` provider:

    REKAI_API_URL=http://localhost:8000 python python/semantic_search.py
    REKAI_API_URL=http://localhost:8000 python python/semantic_search.py "how do I cache?"

For real semantic quality use an embeddings model + key, e.g.

    MODEL=text-embedding-3-small REKAI_PROVIDER_KEY=sk-... \
    python python/semantic_search.py "reduce latency and cost"
"""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.request

API_URL = os.environ.get("REKAI_API_URL", "http://localhost:8000")
MODEL = os.environ.get("MODEL", "echo")

DOCUMENTS = [
    "RekAI caches identical responses in Redis to cut cost and latency.",
    "Bring your own key (BYOK): pass X-Provider-Key per request; it is never stored.",
    "The router picks a provider by explicit choice, model prefix, or the default.",
    "Streaming uses Server-Sent Events at /v1/chat/stream for token-by-token output.",
    "Embeddings turn text into vectors at /v1/embeddings for search and clustering.",
    "Fallback retries a chain of providers when an upstream returns a 5xx error.",
]


def embed(inputs: list[str]) -> list[list[float]]:
    body = {"model": MODEL, "input": inputs}
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("REKAI_PROVIDER_KEY")
    if key:
        headers["X-Provider-Key"] = key
    req = urllib.request.Request(
        f"{API_URL}/v1/embeddings", data=json.dumps(body).encode(), headers=headers
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)["embeddings"]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def main() -> None:
    query = " ".join(sys.argv[1:]) or "how does RekAI save money on repeated calls?"

    # Embed the corpus and the query (the query rides along as the last input).
    vectors = embed(DOCUMENTS + [query])
    doc_vectors, query_vector = vectors[:-1], vectors[-1]

    ranked = sorted(
        ((cosine(query_vector, dv), doc) for dv, doc in zip(doc_vectors, DOCUMENTS)),
        key=lambda pair: pair[0],
        reverse=True,
    )

    print(f"Query: {query}\n")
    print("Most relevant documents:")
    for rank, (score, doc) in enumerate(ranked[:3], start=1):
        print(f"  {rank}. ({score:.3f}) {doc}")


if __name__ == "__main__":
    main()
