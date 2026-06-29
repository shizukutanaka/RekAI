"""A keyless provider used for local development, demos and tests.

It echoes the last user message back, so the whole stack works end-to-end
without any external API or credentials.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

from rekai.providers.base import EmbeddingResult, Provider, ProviderResult, StreamEvent
from rekai.schemas import ChatRequest, Usage

_EMBED_DIM = 16


def _count_tokens(text: str) -> int:
    # Deliberately naive — good enough for a demo provider.
    return max(1, len(text.split()))


def _embed_text(text: str, dim: int = _EMBED_DIM) -> list[float]:
    """A deterministic pseudo-embedding from a hash — no model needed for demos/tests."""
    digest = hashlib.sha256(text.encode()).digest()
    return [digest[i % len(digest)] / 255.0 for i in range(dim)]


class EchoProvider(Provider):
    name = "echo"
    requires_key = False

    async def chat(self, request: ChatRequest, api_key: str | None) -> ProviderResult:
        last_user = next(
            (m.content for m in reversed(request.messages) if m.role == "user" and m.content),
            request.messages[-1].content or "",
        )
        content = f"Echo: {last_user}"
        prompt_tokens = sum(_count_tokens(m.content or "") for m in request.messages)
        completion_tokens = _count_tokens(content)
        return ProviderResult(
            content=content,
            model=request.model,
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )

    async def stream(self, request: ChatRequest, api_key: str | None) -> AsyncIterator[str]:
        async for ev in self.stream_events(request, api_key):
            if ev.delta:
                yield ev.delta

    async def stream_events(
        self, request: ChatRequest, api_key: str | None
    ) -> AsyncIterator[StreamEvent]:
        result = await self.chat(request, api_key)
        # Emit word by word so the streaming path is visibly incremental.
        words = result.content.split(" ")
        for i, word in enumerate(words):
            yield StreamEvent(delta=word if i == 0 else " " + word)
        # echo computes exact usage, so report it (not an estimate).
        yield StreamEvent(usage=result.usage)

    async def embed(self, inputs: list[str], model: str, api_key: str | None) -> EmbeddingResult:
        tokens = sum(_count_tokens(t) for t in inputs)
        return EmbeddingResult(
            embeddings=[_embed_text(t) for t in inputs],
            model=model,
            usage=Usage(prompt_tokens=tokens, total_tokens=tokens),
        )

    async def list_models(self, api_key: str | None) -> list[str]:
        return ["echo"]

    async def list_embedding_models(self, api_key: str | None) -> list[str]:
        return ["echo"]
