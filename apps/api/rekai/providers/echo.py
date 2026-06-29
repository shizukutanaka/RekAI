"""A keyless provider used for local development, demos and tests.

It echoes the last user message back, so the whole stack works end-to-end
without any external API or credentials.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from rekai.providers.base import Provider, ProviderResult
from rekai.schemas import ChatRequest, Usage


def _count_tokens(text: str) -> int:
    # Deliberately naive — good enough for a demo provider.
    return max(1, len(text.split()))


class EchoProvider(Provider):
    name = "echo"
    requires_key = False

    async def chat(self, request: ChatRequest, api_key: str | None) -> ProviderResult:
        last_user = next(
            (m.content for m in reversed(request.messages) if m.role == "user"),
            request.messages[-1].content,
        )
        content = f"Echo: {last_user}"
        prompt_tokens = sum(_count_tokens(m.content) for m in request.messages)
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
        result = await self.chat(request, api_key)
        # Emit word by word so the streaming path is visibly incremental.
        words = result.content.split(" ")
        for i, word in enumerate(words):
            yield word if i == 0 else " " + word

    async def list_models(self, api_key: str | None) -> list[str]:
        return ["echo"]
