"""Gateway authentication.

Optional client auth for the gateway itself (distinct from BYOK, which is the
*upstream* provider key). When one or more gateway keys are configured, `/v1/*`
requests must present ``Authorization: Bearer <key>``. Comparison is
constant-time to avoid leaking a valid key via timing. With no keys configured,
the gateway is open (the default — convenient for local/echo use).
"""

from __future__ import annotations

import hashlib
import secrets


def parse_bearer(authorization: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` header."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def _comparable(value: str) -> bytes:
    """Encode a key for :func:`secrets.compare_digest`.

    Comparison happens on **bytes**, not ``str``. ``compare_digest`` rejects a
    ``str`` containing any non-ASCII character with a ``TypeError``, and the
    token here is attacker-controlled: an unauthenticated request carrying
    ``Authorization: Bearer ké`` raised straight out of the auth middleware as
    an unhandled 500 instead of a 401. Because that happened *before* the rate
    limiter, such requests consumed no budget, went uncounted in
    ``rekai_errors_by_kind_total``, and wrote a full stack trace each time —
    unmetered log amplification from a one-character header change.

    ``surrogatepass`` so a lone surrogate (which strict UTF-8 rejects) can't
    reintroduce the same crash by another route. Both sides go through this, so
    byte equality still means string equality.
    """
    return value.encode("utf-8", "surrogatepass")


def client_id(token: str) -> str:
    """A short, stable, non-reversible id for a key — safe to log and to use as a
    per-tenant rate-limit bucket (never the raw key)."""
    return "key:" + hashlib.sha256(_comparable(token)).hexdigest()[:12]


def key_allowed(token: str, allowed: list[str]) -> bool:
    """True if ``token`` matches any allowed key (constant-time per comparison)."""
    candidate = _comparable(token)
    result = False
    for key in allowed:
        # No early return: compare against every key so timing doesn't reveal
        # which (or how many) keys matched.
        if secrets.compare_digest(candidate, _comparable(key)):
            result = True
    return result
