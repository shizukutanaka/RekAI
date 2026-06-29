"""Official Python client for the RekAI gateway."""

from rekai_client.client import ChatResult, EmbeddingsResult, RekAIClient, RekAIError

__version__ = "1.1.0"
__all__ = ["RekAIClient", "RekAIError", "ChatResult", "EmbeddingsResult"]
