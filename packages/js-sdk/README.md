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

// Streaming (with optional usage/cost + tool-call callbacks)
for await (const chunk of client.stream("echo", "stream me", {
  onUsage: (s) => console.log("\n", s),
  onToolCalls: (calls) => console.log("tools:", calls), // fired if the model calls tools
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

// Structured output (OpenAI/OpenAI-compatible natively, Gemini best-effort)
await client.chat("gpt-4o-mini", "Return JSON with a 'city' field for Tokyo.", {
  responseFormat: { type: "json_object" },
});

// Embeddings (echo works keyless; OpenAI-compatible call the real API)
const emb = await client.embeddings("echo", ["hello", "world"]);
console.log(emb.embeddings.length, emb.usage.total_tokens, emb.cached);

// Introspection
await client.models();   // [{ id, provider }, ...]
await client.usage();    // aggregate counters
await client.health();
```

Set a default BYOK key for every request:

```js
const client = new RekAIClient("http://localhost:8000", { providerKey: "sk-..." });
```

If the deployment has `REKAI_API_KEYS` configured, pass the gateway key too —
distinct from `providerKey` above (that's BYOK for the upstream provider; this
authenticates you to RekAI itself):

```js
const client = new RekAIClient("http://localhost:8000", { gatewayKey: "sk-rekai-..." });
```

## Idempotency & retries

The client retries transient failures — network errors and `429`/`502`/`503`/
`504` responses — with exponential backoff, honoring a `Retry-After` header when
present. Tune or disable it per client:

```js
const client = new RekAIClient("http://localhost:8000", {
  maxRetries: 2,       // default; set 0 to disable
  retryBackoff: 0.5,   // seconds, doubled each attempt
});
```

So an automatic retry can never double-execute a chat, `chat()` sends an
`Idempotency-Key` header — the server replays the first response instead of
re-processing. Pass your own for end-to-end safety, or let the client mint one
per call when retries are enabled:

```js
await client.chat("gpt-4o-mini", "charge me once", { idempotencyKey: "order-42" });
```

## Errors

Failed requests throw `RekAIError` with a `.statusCode` property.

## Test

```bash
cd packages/js-sdk
npm test     # node --test (no extra dependencies)
```
