"""Optional helpers for encrypting provider keys at rest.

BYOK in RekAI is transient by default — keys are passed per request and never
stored. These helpers exist only for deployments that explicitly choose to
persist keys (e.g. a server-side key vault) and want them encrypted.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


def generate_key() -> str:
    """Generate a new Fernet key (base64, 32 bytes)."""
    return Fernet.generate_key().decode()


class KeyCipher:
    """Symmetric encryption for secrets using Fernet (AES-128-CBC + HMAC)."""

    def __init__(self, secret: str) -> None:
        self._fernet = Fernet(secret.encode() if isinstance(secret, str) else secret)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Could not decrypt value: invalid token or wrong key.") from exc


def mask_key(key: str | None) -> str:
    """Return a log-safe representation of an API key."""
    if not key:
        return "<none>"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-4:]}"
