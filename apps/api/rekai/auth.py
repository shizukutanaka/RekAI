"""Gateway authentication.

Optional client auth for the gateway itself (distinct from BYOK, which is the
*upstream* provider key). When one or more gateway keys are configured, `/v1/*`
requests must present ``Authorization: Bearer <key>``. Comparison is
constant-time to avoid leaking a valid key via timing. With no keys configured,
the gateway is open (the default — convenient for local/echo use).
"""

from __future__ import annotations

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


def key_allowed(token: str, allowed: list[str]) -> bool:
    """True if ``token`` matches any allowed key (constant-time per comparison)."""
    result = False
    for key in allowed:
        # No early return: compare against every key so timing doesn't reveal
        # which (or how many) keys matched.
        if secrets.compare_digest(token, key):
            result = True
    return result
