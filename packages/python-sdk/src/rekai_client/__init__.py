"""Official Python client for the RekAI gateway."""

from rekai_client.client import (
    APIConnectionError,
    APITimeoutError,
    AsyncRekAIClient,
    AuthenticationError,
    ChatResult,
    ConflictError,
    EmbeddingsResult,
    InternalServerError,
    ModerationResult,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    RekAIClient,
    RekAIError,
    UnprocessableEntityError,
)

__version__ = "1.3.1"
__all__ = [
    "RekAIClient",
    "AsyncRekAIClient",
    "RekAIError",
    "AuthenticationError",
    "PermissionDeniedError",
    "NotFoundError",
    "ConflictError",
    "UnprocessableEntityError",
    "RateLimitError",
    "InternalServerError",
    "APITimeoutError",
    "APIConnectionError",
    "ChatResult",
    "EmbeddingsResult",
    "ModerationResult",
]
