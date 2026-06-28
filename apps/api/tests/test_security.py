import pytest

from rekai.rate_limit import RateLimiter
from rekai.security import KeyCipher, generate_key, mask_key


def test_cipher_roundtrip() -> None:
    cipher = KeyCipher(generate_key())
    token = cipher.encrypt("sk-secret")
    assert token != "sk-secret"
    assert cipher.decrypt(token) == "sk-secret"


def test_cipher_wrong_key_fails() -> None:
    token = KeyCipher(generate_key()).encrypt("x")
    with pytest.raises(ValueError):
        KeyCipher(generate_key()).decrypt(token)


@pytest.mark.parametrize(
    "key,expected",
    [(None, "<none>"), ("short", "*****"), ("sk-1234567890", "sk-1…7890")],
)
def test_mask_key(key, expected) -> None:
    assert mask_key(key) == expected


def test_rate_limiter_blocks_after_capacity() -> None:
    limiter = RateLimiter(capacity=2, window=60)
    assert limiter.allow("client") is True
    assert limiter.allow("client") is True
    assert limiter.allow("client") is False
    # A different client has its own bucket.
    assert limiter.allow("other") is True
