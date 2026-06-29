# @rekai/client

Official JavaScript/TypeScript client for the
[RekAI](https://github.com/shizukutanaka/RekAI) gateway. Zero dependencies —
uses the global `fetch` (Node 18+ or any modern browser). Ships with TypeScript
type declarations.

## Install

```bash
npm install @rekai/client        # once published
# or, from the monorepo, import from packages/js-sdk/src/index.js
```

## Usage

```js
import { RekAIClient } from "@rekai/client";

const client = new RekAIClient("http://localhost:8000");

// Simple chat (a string becomes a single user message)
const res = await client.chat("echo", "Hello!");
console.log(res.content);                 // "Echo: Hello!"
console.log(res.provider, res.usage.total_tokens, res.cost_usd);

// BYOK + a real provider
await client.chat("gpt-4o-mini", [{ role: "user", content: "Write a haiku." }], {
  provider: "openai",
  providerKey: "sk-...",
});

// Streaming (with optional usage/cost callback)
for await (const chunk of client.stream("echo", "stream me", {
  onUsage: (s) => console.log("\n", s),
})) {
  process.stdout.write(chunk);
}

// Reliability: fall back to echo on upstream errors
await client.chat("gpt-4o-mini", "hi", {
  fallbacks: [{ provider: "echo", model: "echo" }],
});

// Tool / function calling (OpenAI-compatible)
const res = await client.chat("gpt-4o-mini", "weather in Tokyo?", {
  tools: [{ type: "function", function: { name: "get_weather" } }],
  toolChoice: "auto",
});
console.log(res.tool_calls); // the model's requested tool calls, if any

// Introspection
await client.models();   // [{ id, provider }, ...]
await client.usage();    // aggregate counters
await client.health();
```

Set a default BYOK key for every request:

```js
const client = new RekAIClient("http://localhost:8000", { providerKey: "sk-..." });
```

## Errors

Failed requests throw `RekAIError` with a `.statusCode` property.

## Test

```bash
cd packages/js-sdk
npm test     # node --test (no extra dependencies)
```
