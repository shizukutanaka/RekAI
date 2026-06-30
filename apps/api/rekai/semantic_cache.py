"""Semantic response cache.

Exact-match caching misses paraphrases ("hi" vs "hello there"), which keeps hit
rates low for natural-language queries. A *semantic* cache instead embeds the
prompt and reuses a stored response when an earlier prompt's embedding is within
a cosine-similarity threshold — the approach from the GPT Semantic Cache work
(arXiv:2411.05276), which reports large reductions in upstream calls.

Process-local and bounded (FIFO eviction); entries are scoped to a ``bucket``
(provider/model/params) so a hit never crosses model or temperature. Opt-in via
``REKAI_SEMANTIC_CACHE_ENABLED`` — it costs one embedding call per request and
only helps with a real embeddings model.
"""

from __future__ import annotations

import math
from collections import deque


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class SemanticCache:
    def __init__(self, max_entries: int = 1000) -> None:
        # (bucket, embedding, response_json), newest at the right.
        self._entries: deque[tuple[str, list[float], str]] = deque(maxlen=max_entries)

    def find(self, bucket: str, embedding: list[float], threshold: float) -> str | None:
        """Return the stored response whose embedding is most similar to
        ``embedding`` within ``bucket`` and at/above ``threshold`` — else None."""
        best_json: str | None = None
        best_sim = threshold
        for b, emb, payload in self._entries:
            if b != bucket:
                continue
            sim = cosine_similarity(embedding, emb)
            if sim >= best_sim:
                best_sim = sim
                best_json = payload
        return best_json

    def add(self, bucket: str, embedding: list[float], response_json: str) -> None:
        self._entries.append((bucket, embedding, response_json))

    def clear(self) -> None:
        self._entries.clear()


# Module-level singleton (mirrors rekai.metrics.metrics).
semantic_cache = SemanticCache()
