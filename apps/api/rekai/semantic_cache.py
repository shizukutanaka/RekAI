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

**Lookup cost is not free and is not hidden.** A lookup is a linear scan of the
bucket, so it runs in entries × dimensions. That much is inherent to an exact
nearest-neighbour search over a process-local store — the alternative is an ANN
index, which is a lot of machinery for a 1000-entry in-memory cache.

What *was* wasteful is fixed here. Measured on 1000 entries of 1536-dim vectors
(the size of `text-embedding-3-small`), per lookup:

===============================================  =========
originally: cosine re-deriving both norms/entry  ~124 ms
unit-normalized on insert → bare dot product      ~48 ms
…with NumPy doing the dot product                ~2.1 ms
===============================================  =========

NumPy is used only if it is installed; it is **not** a runtime dependency, and
the pure-Python path is the reference implementation (the test suite runs both
and asserts they agree). What remains is a real per-request cost — paid on
misses too, which scan everything and get nothing — so it is measured
(``rekai_semantic_cache_lookup_seconds``) rather than assumed away. At ~48 ms a
lookup, a pure-Python deployment fronting a fast provider can easily spend more
than the cache saves; size ``REKAI_SEMANTIC_CACHE_MAX_ENTRIES`` against that
histogram rather than against the default.
"""

from __future__ import annotations

import math
import re
import time
from collections import deque
from typing import Any

try:  # Optional acceleration; the pure-Python path below is the reference.
    import numpy  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised by whichever env lacks numpy
    numpy = None  # type: ignore[assignment]


# A prepared (unit-normalized) vector: a NumPy array when NumPy is installed,
# otherwise a plain list. Only _prepare produces these and only _similarity
# consumes them, so the two representations never meet.
Vector = Any


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two raw (un-normalized) vectors.

    Kept as the reference definition and for callers outside the cache; the
    cache itself normalizes on insert and uses :func:`_similarity`, because
    this recomputes *both* norms on every comparison — including the stored
    vector's, once per entry per lookup, which is pure waste in a linear scan.
    """
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _prepare(vec: list[float]) -> Vector:
    """Unit-normalize once, so every later comparison is a plain dot product.

    A zero vector is left alone: it has no direction to normalize to, and its
    dot product with anything is 0, which is what the cosine of an undefined
    direction should degrade to here (and matches ``cosine_similarity``)."""
    if numpy is not None:
        arr = numpy.asarray(vec, dtype=numpy.float64)
        norm = float(numpy.linalg.norm(arr))
        return arr / norm if norm else arr
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else list(vec)


def _similarity(query: Vector, stored: Vector) -> float:
    """Cosine similarity of two already-normalized vectors, i.e. their dot product.

    Rounded to 12 decimals and clamped to [-1, 1]. Summing 1536 float64 products
    accumulates error around 1e-16, which leaves a vector's similarity to
    *itself* at 0.9999999999999998 — so a threshold of exactly 1.0 ("only an
    identical embedding may hit") would never match anything. Rounding well
    above the noise floor and far below any meaningful discrimination makes the
    identity case exact without affecting a real comparison.
    """
    if len(query) != len(stored) or len(query) == 0:
        return 0.0
    if numpy is not None:
        dot = float(query @ stored)
    else:
        dot = sum(x * y for x, y in zip(query, stored, strict=True))
    return max(-1.0, min(1.0, round(dot, 12)))


# --- discriminator guard -----------------------------------------------------
# Cosine similarity is not a proof that two prompts have the same answer, and it
# fails hardest on small edits that flip meaning — which is exactly what a
# paraphrase-hunting cache is built to ignore. GPTCache (arXiv:2311.13133) puts
# a second, more discriminative model after the vector search for this reason.
# A second model call is a heavy price for an in-process cache, so RekAI checks
# only the two features that most reliably change an answer while barely moving
# an embedding:
#
#   negation  "is aspirin safe in pregnancy" vs "is aspirin not safe in
#             pregnancy" — near-identical vectors, opposite question. Embedding
#             models are well known to under-represent negation.
#   numbers   "convert 5 USD to EUR" vs "convert 500 USD to EUR"; "summarize
#             invoice 12345" vs "invoice 12346".
#
# The check can only turn a hit into a miss, never the reverse, and the costs
# are asymmetric: a wrong miss costs one upstream call, a wrong hit returns the
# wrong answer to a question nobody asked. Most conversational prompts contain
# no numbers and no negation, so on that traffic the guard is inert.
#
# Only the *digest* is stored, never the prompt text — the semantic cache is not
# a place prompts should accumulate.

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_NEGATION_RE = re.compile(
    r"n't\b|\b(?:not|never|cannot|without|neither|nor|no|none|nothing|nobody|nowhere)\b",
    re.IGNORECASE,
)


def discriminators(text: str) -> tuple[tuple[str, ...], int]:
    """A tiny digest of the meaning-flipping features of ``text``.

    Returns the numeric literals **in order** (order matters: "convert 5 to 10"
    and "convert 10 to 5" are different questions with the same multiset) and
    the count of negation markers. Numbers are normalized so ``5`` and ``5.0``
    agree.
    """
    numbers = tuple(_normalize_number(m) for m in _NUMBER_RE.findall(text))
    return numbers, len(_NEGATION_RE.findall(text))


def _normalize_number(literal: str) -> str:
    try:
        return repr(float(literal))
    except ValueError:  # pragma: no cover - the regex only matches numerics
        return literal


class SemanticCache:
    def __init__(self, max_entries: int = 1000) -> None:
        # (bucket, normalized embedding, discriminators, response_json, expires_at),
        # newest right.
        self._entries: deque[tuple[str, Vector, tuple[tuple[str, ...], int], str, float]] = deque(
            maxlen=max_entries
        )

    def find(
        self,
        bucket: str,
        prompt: str,
        embedding: list[float],
        threshold: float,
        now: float | None = None,
    ) -> tuple[str, float] | None:
        """Return ``(stored_response, similarity)`` for the closest entry in
        ``bucket`` at/above ``threshold``, else None. Expired entries are
        ignored (they age out of the deque on their own).

        ``prompt`` is used only for the discriminator guard above — entries
        whose numbers or negation count differ are rejected however close their
        embeddings are.

        The similarity comes back with the payload rather than being discarded
        because the caller has to disclose it: a hit at 0.999 and a hit at
        exactly the threshold are very different claims about the answer, and
        without the number a caller cannot tell a semantic hit from an exact
        one at all.
        """
        moment = time.time() if now is None else now
        # Normalize the query once, not once per entry.
        query = _prepare(embedding)
        marks = discriminators(prompt)
        best: tuple[str, float] | None = None
        best_sim = threshold
        for b, emb, entry_marks, payload, expires_at in self._entries:
            if b != bucket or expires_at <= moment:
                continue
            if entry_marks != marks:
                continue
            if len(emb) != len(query):
                # A different embedding dimension means a different model —
                # change REKAI_SEMANTIC_CACHE_MODEL and old entries linger in
                # this process-local store. Their coordinates mean nothing in
                # the new model's space, so skip rather than score: scoring
                # them yields 0.0, which with a threshold of 0.0 is a *match*.
                continue
            sim = _similarity(query, emb)
            if sim >= best_sim:
                best_sim = sim
                best = (payload, sim)
        return best

    def add(
        self,
        bucket: str,
        prompt: str,
        embedding: list[float],
        response_json: str,
        ttl: int,
        now: float | None = None,
    ) -> None:
        """Store a response under ``bucket`` for ``ttl`` seconds.

        Only ``prompt``'s discriminator digest is retained, never its text.

        A ttl of 0 stores nothing: the response cache treats
        ``REKAI_CACHE_TTL_SECONDS=0`` as "don't cache", and a semantic entry
        that outlived the exact one it mirrors would be the more surprising of
        the two to keep."""
        if ttl <= 0:
            return
        moment = time.time() if now is None else now
        self._entries.append(
            (bucket, _prepare(embedding), discriminators(prompt), response_json, moment + ttl)
        )

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
