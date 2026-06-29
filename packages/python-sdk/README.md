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

## Errors

Failed requests raise `RekAIError` with a `.status_code` attribute.

## Develop

```bash
cd packages/python-sdk
pip install -e ".[dev]"
ruff check . && pytest
```
