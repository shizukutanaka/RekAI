#!/usr/bin/env node
// Stream a RekAI chat completion (SSE) using the built-in fetch (Node 18+).
//
// Usage:
//   node javascript/stream.mjs "your prompt here"
//
// Environment:
//   REKAI_API_URL        API base URL (default http://localhost:8000)
//   MODEL                model to request (default "echo")
//   REKAI_PROVIDER_KEY   optional BYOK key, sent as X-Provider-Key
//   REKAI_GATEWAY_KEY    optional gateway key, sent as Authorization: Bearer
//                        (only needed if the deployment has REKAI_API_KEYS set)

const API_URL = process.env.REKAI_API_URL || "http://localhost:8000";
const MODEL = process.env.MODEL || "echo";

async function stream(prompt) {
  const headers = { "Content-Type": "application/json" };
  if (process.env.REKAI_PROVIDER_KEY) {
    headers["X-Provider-Key"] = process.env.REKAI_PROVIDER_KEY;
  }
  if (process.env.REKAI_GATEWAY_KEY) {
    headers["Authorization"] = `Bearer ${process.env.REKAI_GATEWAY_KEY}`;
  }

  let res;
  try {
    res = await fetch(`${API_URL}/v1/chat/stream`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        model: MODEL,
        messages: [{ role: "user", content: prompt }],
      }),
    });
  } catch (err) {
    throw new Error(`Could not reach RekAI at ${API_URL}: ${err.message}`);
  }
  if (!res.ok) {
    throw new Error(`API error ${res.status}: ${await res.text()}`);
  }
  if (!res.body) return;

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line; a frame may split across reads.
    let sep;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!dataLine) continue;
      const payload = dataLine.slice("data:".length).trim();
      if (payload === "[DONE]") return;
      let event;
      try {
        event = JSON.parse(payload);
      } catch {
        continue;
      }
      if (event.delta) process.stdout.write(event.delta);
      else if (event.error) {
        throw new Error(`stream error: ${event.detail || event.error}`);
      }
    }
  }
}

const prompt = process.argv.slice(2).join(" ") || "Tell me a short story.";
try {
  await stream(prompt);
  process.stdout.write("\n");
} catch (err) {
  console.error(`\n${err.message}`);
  process.exit(1);
}
