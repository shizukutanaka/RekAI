#!/usr/bin/env node
// Minimal RekAI client using the built-in fetch (Node 18+).
//
// Usage:
//   node javascript/chat.mjs "your prompt here"
//
// Environment:
//   REKAI_API_URL        API base URL (default http://localhost:8000)
//   MODEL                model to request (default "echo")
//   REKAI_PROVIDER_KEY   optional BYOK key, sent as X-Provider-Key

const API_URL = process.env.REKAI_API_URL || "http://localhost:8000";
const MODEL = process.env.MODEL || "echo";

async function chat(prompt) {
  const headers = { "Content-Type": "application/json" };
  if (process.env.REKAI_PROVIDER_KEY) {
    headers["X-Provider-Key"] = process.env.REKAI_PROVIDER_KEY;
  }

  let res;
  try {
    res = await fetch(`${API_URL}/v1/chat`, {
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
  return res.json();
}

const prompt = process.argv.slice(2).join(" ") || "Hello from JavaScript!";
try {
  const result = await chat(prompt);
  console.log(result.content);
  console.log(
    `\n[provider=${result.provider} model=${result.model} ` +
      `cached=${result.cached} tokens=${result.usage.total_tokens}]`,
  );
} catch (err) {
  console.error(err.message);
  process.exit(1);
}
