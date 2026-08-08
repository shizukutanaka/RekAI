"""Idempotency-Key support.

A client can send an ``Idempotency-Key`` header (a unique id, e.g. a UUID) with a
write request. If it retries the same operation with the same key, RekAI returns
the **stored response** from the first call instead of processing it again — so a
network blip or an automatic retry can't double-charge or duplicate work.

Semantics (Stripe-style), enforced per key:

* **Replay** — a completed request replayed with the *same* body returns the
  stored response (``Idempotent-Replay: true``).
* **Body mismatch** — the same key with a *different* request body is a client
  error (the caller reused a key for a new operation); the route returns 422.
  Each stored record carries a sha256 ``fingerprint`` of the request body.
* **In flight** — a second request with the same key that arrives while the
  first is still being processed returns 409, rather than racing it. The key is
  claimed atomically with an in-progress sentinel (``cache.add`` → Redis
  ``SET NX`` / an event-loop-atomic memory write) so two concurrent requests
  can't both proceed.

Records are scoped **per client**, not globally: the store key mixes the caller's
client id (the masked API-key id under gateway auth, else the client IP) with the
header value. Idempotency keys are chosen by callers and collide readily — Stripe
scopes them per API key for exactly this reason — so a global namespace would let
one tenant's ``Idempotency-Key: req-1`` replay another tenant's stored response,
or claim the sentinel first and 409 them out of their own key.

Keys live in the response cache backend (Redis/memory) under a separate
namespace; when caching is disabled (NullCache) every claim "succeeds" and the
store never persists, so idempotency degrades to a no-op. All cache access is
best-effort: a backend error fails **open** (the request proceeds without
idempotency protection) rather than failing the call, matching the rest of the
codebase's Redis posture.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from rekai.cache import CacheBackend

_PREFIX = "rekai:idem:"
_IN_PROGRESS = "in_progress"
_DONE = "done"


def _store_key(client_id: str, raw_key: str) -> str:
    """Namespace a client's idempotency key to that client.

    The two parts are length-prefixed before hashing so no pair of
    ``(client_id, raw_key)`` values can be rearranged into the same digest.
    """
    material = f"{len(client_id)}:{client_id}:{raw_key}"
    return _PREFIX + hashlib.sha256(material.encode()).hexdigest()


def fingerprint(body: str) -> str:
    """A stable sha256 of the serialized request body, for same-key/diff-body
    detection. Pass a canonical serialization (e.g. ``model_dump_json()``)."""
    return hashlib.sha256(body.encode()).hexdigest()


@dataclass
class Outcome:
    """The result of claiming an idempotency key before processing.

    ``kind`` is one of:

    * ``"proceed"`` — the key was claimed (or idempotency is disabled); process
      the request, then call :func:`complete` (or :func:`release` on error).
    * ``"replay"`` — a completed record with a matching body exists;
      ``response`` holds its stored payload to return verbatim.
    * ``"mismatch"`` — the key exists with a different body fingerprint → 422.
    * ``"conflict"`` — a request with this key is still in progress → 409.
    """

    kind: str
    response: dict[str, Any] | None = None


async def _get(cache: CacheBackend, client_id: str, raw_key: str) -> dict[str, Any] | None:
    try:
        stored = await cache.get(_store_key(client_id, raw_key))
    except Exception:  # pragma: no cover - fail open on backend error
        return None
    if stored is None:
        return None
    try:
        return json.loads(stored)
    except json.JSONDecodeError:  # pragma: no cover - defensive
        return None


async def claim(
    cache: CacheBackend, client_id: str, raw_key: str, body_fingerprint: str, ttl: int
) -> Outcome:
    """Atomically claim ``raw_key`` for ``client_id``, or classify an existing record.

    Call once before doing the work. On ``"proceed"`` the caller holds the
    in-progress sentinel and must finish with :func:`complete` or :func:`release`.
    """
    sentinel = json.dumps({"status": _IN_PROGRESS, "fingerprint": body_fingerprint})
    try:
        claimed = await cache.add(_store_key(client_id, raw_key), sentinel, ttl)
    except Exception:  # pragma: no cover - fail open on backend error
        return Outcome("proceed")
    if claimed:
        return Outcome("proceed")

    # Someone else got here first (an in-progress sentinel or a completed
    # record). Read it back to decide replay / mismatch / conflict.
    existing = await _get(cache, client_id, raw_key)
    if existing is None:
        # The record vanished between the failed claim and this read (TTL
        # expiry, eviction). Rare; proceed best-effort without the sentinel.
        return Outcome("proceed")
    if existing.get("fingerprint") != body_fingerprint:
        return Outcome("mismatch")
    if existing.get("status") == _IN_PROGRESS:
        return Outcome("conflict")
    return Outcome("replay", existing.get("response"))


async def complete(
    cache: CacheBackend,
    client_id: str,
    raw_key: str,
    body_fingerprint: str,
    response: dict[str, Any],
    ttl: int,
) -> None:
    """Persist the final response, replacing the in-progress sentinel."""
    record = json.dumps({"status": _DONE, "fingerprint": body_fingerprint, "response": response})
    try:
        await cache.set(_store_key(client_id, raw_key), record, ttl)
    except Exception:  # pragma: no cover - fail open on backend error
        pass


async def release(cache: CacheBackend, client_id: str, raw_key: str) -> None:
    """Drop the in-progress sentinel so a failed request can be retried at once
    (instead of being blocked by its own stale sentinel until TTL)."""
    try:
        await cache.delete(_store_key(client_id, raw_key))
    except Exception:  # pragma: no cover - fail open on backend error
        pass
