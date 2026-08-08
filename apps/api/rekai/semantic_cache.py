"""Semantic response cache.

Exact-match caching misses paraphrases ("hi" vs "hello there"), which keeps hit
rates low for natural-language queries. A *semantic* cache instead embeds the
prompt and reuses a stored response when an earlier prompt's embedding is within
a cosine-similarity threshold — the approach from the GPT Semantic Cache work
(arXiv:2411.05276), which reports large reductions in upstream calls.

Process-local, bounded (FIFO eviction), and TTL'd. Unlike the exact cache, a hit
here answers a prompt that was **never sent**, so what counts as "the same
question" has to be conservative:

* Entries are scoped to a ``bucket`` (see :func:`rekai.cache.semantic_bucket`)
  covering provider, model, sampling params, tools, ``response_format``,
  ``cache_control`` — and the **requesting client**. Cross-client reuse would
  hand tenant B an answer to a prompt only tenant A asked; the exact cache can
  share freely because a hit there requires B to have sent the identical prompt
  itself.
* The threshold is only as meaningful as the embedding behind it. A hash-based
  stand-in (the ``echo`` provider) is not semantic at all and produces frequent
  false hits — ``create_app`` warns loudly if one is configured.

Opt-in via ``REKAI_SEMANTIC_CACHE_ENABLED``; it costs one embedding call per
request and needs a real embeddings model.
"""

from __future__ import annotations

import math
import time
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
        # (bucket, embedding, response_json, expires_at), newest at the right.
        self._entries: deque[tuple[str, list[float], str, float]] = deque(maxlen=max_entries)

    def find(
        self, bucket: str, embedding: list[float], threshold: float, now: float | None = None
    ) -> tuple[str, float] | None:
        """Return ``(stored_response, similarity)`` for the closest entry in
        ``bucket`` at/above ``threshold``, else None. Expired entries are
        ignored (they age out of the deque on their own).

        The similarity comes back with the payload rather than being discarded
        because the caller has to disclose it: a hit at 0.999 and a hit at
        exactly the threshold are very different claims about the answer, and
        without the number a caller cannot tell a semantic hit from an exact
        one at all.
        """
        moment = time.time() if now is None else now
        best: tuple[str, float] | None = None
        best_sim = threshold
        for b, emb, payload, expires_at in self._entries:
            if b != bucket or expires_at <= moment:
                continue
            sim = cosine_similarity(embedding, emb)
            if sim >= best_sim:
                best_sim = sim
                best = (payload, sim)
        return best

    def add(
        self,
        bucket: str,
        embedding: list[float],
        response_json: str,
        ttl: int,
        now: float | None = None,
    ) -> None:
        """Store a response under ``bucket`` for ``ttl`` seconds.

        A ttl of 0 stores nothing: the response cache treats
        ``REKAI_CACHE_TTL_SECONDS=0`` as "don't cache", and a semantic entry
        that outlived the exact one it mirrors would be the more surprising of
        the two to keep."""
        if ttl <= 0:
            return
        moment = time.time() if now is None else now
        self._entries.append((bucket, embedding, response_json, moment + ttl))

    def clear(self) -> None:
        self._entries.clear()

    def resize(self, max_entries: int) -> None:
        """Apply ``REKAI_SEMANTIC_CACHE_MAX_ENTRIES`` to this singleton.

        The deque's ``maxlen`` is immutable, so this rebuilds it, keeping the
        newest entries. Called from ``create_app`` for the same reason
        ``metrics.max_tracked_clients`` is: the singleton predates any Settings
        instance."""
        if max_entries == self._entries.maxlen:
            return
        kept = list(self._entries)[-max_entries:] if max_entries else list(self._entries)
        self._entries = deque(kept, maxlen=max_entries)


# Module-level singleton (mirrors rekai.metrics.metrics).
semantic_cache = SemanticCache()
