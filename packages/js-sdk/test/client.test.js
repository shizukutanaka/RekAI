import assert from "node:assert/strict";
import http from "node:http";
import { after, before, test } from "node:test";
import { RekAIClient, RekAIError } from "../src/index.js";

let server;
let baseUrl;
let lastRequest = {};

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
        if (req.headers["x-provider-key"] === "bad") {
          return send(res, 401, { error: "provider_error", detail: "no key" });
        }
        return send(res, 200, {
          id: "rekai-1",
          provider: "echo",
          model: "echo",
          content: "Echo: hi",
          usage: { prompt_tokens: 1, completion_tokens: 2, total_tokens: 3 },
          cost_usd: 0.0,
          cached: false,
          fallback_used: false,
        });
      }
      if (req.url === "/v1/chat/stream") {
        const sse =
          'data: {"delta": "Hello"}\n\n' +
          'data: {"delta": " world"}\n\n' +
          "data: [DONE]\n\n";
        return send(res, 200, sse, { "Content-Type": "text/event-stream" });
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

test("models, usage, health", async () => {
  const client = new RekAIClient(baseUrl);
  assert.equal((await client.models())[0].id, "echo");
  assert.equal((await client.usage()).requests_total, 7);
  assert.equal((await client.health()).status, "ok");
});
