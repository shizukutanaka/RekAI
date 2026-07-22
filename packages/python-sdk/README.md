# rekai-client

Official Python client for the [RekAI](https://github.com/shizukutanaka/RekAI)
gateway.

## Install

```bash
pip install -e packages/python-sdk     # from the monorepo
# (published to PyPI as `rekai-client` once released)
```

## Usage

```python
from rekai_client import RekAIClient

client = RekAIClient("http://localhost:8000")

# Simple chat (a plain string becomes a single user message)
result = client.chat("echo", "Hello!")
print(result.content)          # "Echo: Hello!"
print(result.provider, result.usage["total_tokens"], result.cost_usd)

# BYOK + a real provider
result = client.chat(
    "gpt-4o-mini",
    [{"role": "user", "content": "Write a haiku."}],
    provider="openai",
    provider_key="sk-...",
)

# Gateway auth: if the deployment has REKAI_API_KEYS configured, pass the
# gateway key too — distinct from provider_key above (that's BYOK for the
# upstream provider; this authenticates you to RekAI itself).
client = RekAIClient("http://localhost:8000", gateway_key="sk-rekai-...")

# Streaming (with optional usage/cost callback)
for chunk in client.stream("echo", "stream me", on_usage=lambda s: print("\n", s)):
    print(chunk, end="", flush=True)

# Reliability: fall back to echo on upstream errors
client.chat("gpt-4o-mini", "hi", fallbacks=[{"provider": "echo", "model": "echo"}])

# Tool / function calling (OpenAI-compatible)
res = client.chat(
    "gpt-4o-mini",
    "weather in Tokyo?",
    tools=[{"type": "function", "function": {"name": "get_weather"}}],
    tool_choice="auto",
)
print(res.tool_calls)  # the model's requested tool calls, if any

# Structured output (OpenAI/OpenAI-compatible natively, Gemini best-effort)
res = client.chat(
    "gpt-4o-mini",
    "Return a JSON object with a 'city' and 'country' field for Tokyo.",
    response_format={"type": "json_object"},
)

# Embeddings (echo works keyless; OpenAI-compatible call the real API)
emb = client.embeddings("echo", ["hello", "world"])
print(len(emb.embeddings), emb.usage["total_tokens"], emb.cached)

# Introspection
client.models()   # [{"id": "...", "provider": "..."}, ...]
client.usage()    # aggregate counters
client.health()
```

Use it as a context manager to close the underlying HTTP connection:

```python
with RekAIClient("http://localhost:8000", provider_key="sk-...") as client:
    print(client.chat("gpt-4o-mini", "hi").content)
```

## Async

`AsyncRekAIClient` mirrors the same surface with `async`/`await`, backed by
`httpx.AsyncClient` (so the connection pool is reused across awaits).
`stream()` is an async generator you drive with `async for`:

```python
import asyncio
from rekai_client import AsyncRekAIClient

async def main():
    async with AsyncRekAIClient("http://localhost:8000") as client:
        result = await client.chat("echo", "Hello!")
        print(result.content)

        async for chunk in client.stream("echo", "stream me"):
            print(chunk, end="", flush=True)

        # on_usage may be a plain callable or a coroutine function.
        await client.embeddings("echo", ["hello", "world"])

asyncio.run(main())
```

## Idempotency & retries

Both clients retry transient failures — connection errors and `429`/`502`/
`503`/`504` responses — with exponential backoff, honoring a `Retry-After`
header when present. Tune or disable it per client:

```python
client = RekAIClient(
    "http://localhost:8000",
    max_retries=2,      # default; set 0 to disable
    retry_backoff=0.5,  # seconds, doubled each attempt
)
```

So an automatic retry can never double-execute a chat, `chat()` sends an
`Idempotency-Key` header — the server replays the first response instead of
re-processing. Pass your own for end-to-end safety, or let the client mint one
per call when retries are enabled:

```python
client.chat("gpt-4o-mini", "charge me once", idempotency_key="order-42")
```

`AsyncRekAIClient` accepts the same options.

## Errors

Failed requests raise `RekAIError` with a `.status_code` attribute.

## Develop

```bash
cd packages/python-sdk
pip install -e ".[dev]"
ruff check . && pytest
```
