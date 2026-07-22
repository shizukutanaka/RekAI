"""Official Python client for the RekAI gateway."""

from rekai_client.client import (
    AsyncRekAIClient,
    ChatResult,
    EmbeddingsResult,
    RekAIClient,
    RekAIError,
)

__version__ = "1.2.0"
__all__ = [
    "RekAIClient",
    "AsyncRekAIClient",
    "RekAIError",
    "ChatResult",
    "EmbeddingsResult",
]
