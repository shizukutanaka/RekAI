import assert from "node:assert/strict";
import http from "node:http";
import { after, before, test } from "node:test";
import { RekAIClient, RekAIError } from "../src/index.js";

let server;
let baseUrl;
let lastRequest = {};
// Test-controlled transient-failure injection for the retry tests. When
// `remaining > 0`, /v1/chat responds with `status` and decrements, so the
// client's retry can eventually reach a 200. Also records the idempotency key
// seen on each attempt.
let flake = { remaining: 0, status: 503, retryAfter: undefined, keys: [] };

function send(res, status, body, headers = {}) {
  res.writeHead(status, { "Content-Type": "application/json", ...headers });
  res.end(typeof body === "string" ? body : JSON.stringify(body));
}

before(async () => {
  server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      lastRequest = {
        method: req.method,
        url: req.url,
        headers: req.headers,
        body: raw ? JSON.parse(raw) : null,
      };

      if (req.url === "/v1/chat") {
        flake.keys.push(req.headers["idempotency-key"]);
        if (flake.remaining > 0) {
          flake.remaining -= 1;
          const h = flake.retryAfter ? { "Retry-After": String(flake.retryAfter) } : {};
          return send(res, flake.status, { detail: "transient" }, h);
        }
        if (req.headers["x-provider-key"] === "bad") {
          return send(res, 401, { error: "provider_error", detail: "no key" });
        }
        return send(res, 200, {
          id: "rekai-1",
          provider: "echo",
          model: "echo",
          content: "Echo: hi",
          tool_calls: lastRequest.body && lastRequest.body.tools ? [{ id: "c1" }] : null,
          usage: { prompt_tokens: 1, completion_tokens: 2, total_tokens: 3 },
          cost_usd: 0.0,
          cached: false,
          fallback_used: false,
        });
      }
      if (req.url === "/v1/chat/stream") {
        // Ride tool_calls on the summary event when the request asked for tools.
        const toolCalls = lastRequest.body && lastRequest.body.tools
          ? ',"tool_calls":[{"id":"c1","type":"function",' +
            '"function":{"name":"get_weather","arguments":"{}"}}]'
          : "";
        const sse =
          'data: {"delta": "Hello"}\n\n' +
          'data: {"delta": " world"}\n\n' +
          'data: {"provider":"echo","model":"echo",' +
          '"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3},' +
          '"cost_usd":0,"estimated":false' + toolCalls + "}\n\n" +
          "data: [DONE]\n\n";
        return send(res, 200, sse, { "Content-Type": "text/event-stream" });
      }
      if (req.url === "/v1/embeddings") {
        return send(res, 200, {
          provider: "echo",
          model: "echo",
          embeddings: [[0.1, 0.2], [0.3, 0.4]],
          usage: { prompt_tokens: 2, completion_tokens: 0, total_tokens: 2 },
          cost_usd: 0.0,
          cached: false,
        });
      }
      if (req.url === "/v1/models") {
        return send(res, 200, { data: [{ id: "echo", provider: "echo" }] });
      }
      if (req.url === "/v1/usage") {
        return send(res, 200, { requests_total: 7 });
      }
      if (req.url === "/health") {
        return send(res, 200, { status: "ok" });
      }
      send(res, 404, { error: "not_found" });
    });
  });
  await new Promise((resolve) => server.listen(0, resolve));
  baseUrl = `http://localhost:${server.address().port}`;
});

after(() => server.close());

test("chat returns result and normalizes a string message", async () => {
  const client = new RekAIClient(baseUrl);
  const result = await client.chat("echo", "hi");
  assert.equal(result.content, "Echo: hi");
  assert.equal(result.usage.total_tokens, 3);
  assert.deepEqual(lastRequest.body.messages, [{ role: "user", content: "hi" }]);
});

test("chat forwards options and BYOK header", async () => {
  const client = new RekAIClient(baseUrl, { providerKey: "sk-default" });
  await client.chat("gpt-4o-mini", [{ role: "user", content: "hi" }], {
    provider: "openai",
    maxTokens: 64,
    fallbacks: [{ provider: "echo" }],
    providerKey: "sk-override",
  });
  assert.equal(lastRequest.body.provider, "openai");
  assert.equal(lastRequest.body.max_tokens, 64);
  assert.deepEqual(lastRequest.body.fallbacks, [{ provider: "echo" }]);
  assert.equal(lastRequest.headers["x-provider-key"], "sk-override");
});

test("chat forwards tools and returns tool_calls", async () => {
  const client = new RekAIClient(baseUrl);
  const tools = [{ type: "function", function: { name: "get_weather" } }];
  const result = await client.chat("gpt-4o-mini", "weather?", {
    tools,
    toolChoice: "auto",
  });
  assert.deepEqual(lastRequest.body.tools, tools);
  assert.equal(lastRequest.body.tool_choice, "auto");
  assert.deepEqual(result.tool_calls, [{ id: "c1" }]);
});

test("chat forwards response_format", async () => {
  const client = new RekAIClient(baseUrl);
  await client.chat("gpt-4o-mini", "give me json", {
    responseFormat: { type: "json_object" },
  });
  assert.deepEqual(lastRequest.body.response_format, { type: "json_object" });
});

test("chat omits response_format when absent", async () => {
  const client = new RekAIClient(baseUrl);
  await client.chat("echo", "hi");
  assert.equal(lastRequest.body.response_format, undefined);
});

test("chat forwards gateway key as a Bearer header", async () => {
  const client = new RekAIClient(baseUrl, { gatewayKey: "sk-rekai-default" });
  await client.chat("echo", "hi");
  assert.equal(lastRequest.headers["authorization"], "Bearer sk-rekai-default");

  await client.chat("echo", "hi", { gatewayKey: "sk-rekai-override" });
  assert.equal(lastRequest.headers["authorization"], "Bearer sk-rekai-override");
});

test("models and usage forward the gateway key", async () => {
  const client = new RekAIClient(baseUrl);
  await client.models({ gatewayKey: "sk-rekai-1" });
  assert.equal(lastRequest.headers["authorization"], "Bearer sk-rekai-1");

  await client.usage({ gatewayKey: "sk-rekai-2" });
  assert.equal(lastRequest.headers["authorization"], "Bearer sk-rekai-2");
});

test("chat raises RekAIError on error status", async () => {
  const client = new RekAIClient(baseUrl, { providerKey: "bad" });
  await assert.rejects(
    () => client.chat("gpt-4o-mini", "hi"),
    (err) => err instanceof RekAIError && err.statusCode === 401,
  );
});

test("stream yields deltas", async () => {
  const client = new RekAIClient(baseUrl);
  const chunks = [];
  for await (const c of client.stream("echo", "hi")) chunks.push(c);
  assert.equal(chunks.join(""), "Hello world");
});

test("stream reports usage via onUsage", async () => {
  const client = new RekAIClient(baseUrl);
  let summary = null;
  const chunks = [];
  for await (const c of client.stream("echo", "hi", { onUsage: (s) => (summary = s) })) {
    chunks.push(c);
  }
  assert.equal(chunks.join(""), "Hello world");
  assert.ok(summary);
  assert.equal(summary.usage.total_tokens, 3);
  assert.equal(summary.estimated, false);
});

test("stream surfaces tool_calls via onToolCalls", async () => {
  const client = new RekAIClient(baseUrl);
  let toolCalls = null;
  let summary = null;
  const chunks = [];
  for await (const c of client.stream("echo", "weather?", {
    tools: [{ type: "function", function: { name: "get_weather" } }],
    onUsage: (s) => (summary = s),
    onToolCalls: (t) => (toolCalls = t),
  })) {
    chunks.push(c);
  }
  assert.equal(chunks.join(""), "Hello world");
  assert.ok(toolCalls);
  assert.equal(toolCalls[0].function.name, "get_weather");
  // Still present on the usage summary too (unchanged wire shape).
  assert.equal(summary.tool_calls[0].id, "c1");
});

test("embeddings returns vectors and forwards options", async () => {
  const client = new RekAIClient(baseUrl);
  const result = await client.embeddings("echo", ["a", "b"], {
    provider: "echo",
    providerKey: "sk-e",
  });
  assert.deepEqual(result.embeddings, [[0.1, 0.2], [0.3, 0.4]]);
  assert.equal(result.usage.total_tokens, 2);
  assert.equal(result.cost_usd, 0.0);
  assert.deepEqual(lastRequest.body, { model: "echo", input: ["a", "b"], cache: true, provider: "echo" });
  assert.equal(lastRequest.headers["x-provider-key"], "sk-e");
});

test("embeddings accepts a string input", async () => {
  const client = new RekAIClient(baseUrl);
  await client.embeddings("echo", "hello", { cache: false });
  assert.equal(lastRequest.body.input, "hello");
  assert.equal(lastRequest.body.cache, false);
});

test("models, usage, health", async () => {
  const client = new RekAIClient(baseUrl);
  assert.equal((await client.models())[0].id, "echo");
  assert.equal((await client.usage()).requests_total, 7);
  assert.equal((await client.health()).status, "ok");
});

// --- Idempotency-Key + client-side retry (S-11) ------------------------------

test("chat sends an explicit Idempotency-Key", async () => {
  const client = new RekAIClient(baseUrl);
  await client.chat("echo", "hi", { idempotencyKey: "my-key-123" });
  assert.equal(lastRequest.headers["idempotency-key"], "my-key-123");
});

test("chat auto-generates an Idempotency-Key when retries are enabled", async () => {
  const client = new RekAIClient(baseUrl); // default maxRetries=2
  await client.chat("echo", "hi");
  assert.match(lastRequest.headers["idempotency-key"], /^rekai-sdk-/);
});

test("chat omits the Idempotency-Key when retries are disabled", async () => {
  const client = new RekAIClient(baseUrl, { maxRetries: 0 });
  await client.chat("echo", "hi");
  assert.equal(lastRequest.headers["idempotency-key"], undefined);
});

test("chat retries a 429 and reuses the same Idempotency-Key", async () => {
  flake = { remaining: 1, status: 429, retryAfter: undefined, keys: [] };
  const client = new RekAIClient(baseUrl, { retryBackoff: 0 });
  const result = await client.chat("echo", "hi", { idempotencyKey: "k1" });
  assert.equal(result.content, "Echo: hi");
  assert.deepEqual(flake.keys, ["k1", "k1"]); // one retry, same key
  flake = { remaining: 0, status: 503, retryAfter: undefined, keys: [] };
});

test("chat gives up after maxRetries and raises", async () => {
  flake = { remaining: 5, status: 503, retryAfter: undefined, keys: [] };
  const client = new RekAIClient(baseUrl, { maxRetries: 1, retryBackoff: 0 });
  await assert.rejects(
    () => client.chat("echo", "hi"),
    (err) => err instanceof RekAIError && err.statusCode === 503,
  );
  assert.equal(flake.keys.length, 2); // initial attempt + 1 retry
  flake = { remaining: 0, status: 503, retryAfter: undefined, keys: [] };
});

test("chat retries a network error then succeeds", async () => {
  const realFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (...args) => {
    calls++;
    if (calls === 1) return Promise.reject(new TypeError("network down"));
    return realFetch(...args);
  };
  try {
    const client = new RekAIClient(baseUrl, { retryBackoff: 0 });
    const result = await client.chat("echo", "hi");
    assert.equal(result.content, "Echo: hi");
    assert.equal(calls, 2);
  } finally {
    globalThis.fetch = realFetch;
  }
});
