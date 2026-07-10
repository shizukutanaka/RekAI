#!/usr/bin/env node
// Create text embeddings through RekAI using the built-in fetch (Node 18+).
//
// Usage:
//   node javascript/embeddings.mjs "first text" "second text" ...
//
// Environment:
//   REKAI_API_URL        API base URL (default http://localhost:8000)
//   MODEL                embeddings model (default "echo", keyless)
//   REKAI_PROVIDER_KEY   optional BYOK key, sent as X-Provider-Key
//   REKAI_GATEWAY_KEY    optional gateway key, sent as Authorization: Bearer
//                        (only needed if the deployment has REKAI_API_KEYS set)

const API_URL = process.env.REKAI_API_URL || "http://localhost:8000";
const MODEL = process.env.MODEL || "echo";

async function embed(inputs) {
  const headers = { "Content-Type": "application/json" };
  if (process.env.REKAI_PROVIDER_KEY) {
    headers["X-Provider-Key"] = process.env.REKAI_PROVIDER_KEY;
  }
  if (process.env.REKAI_GATEWAY_KEY) {
    headers["Authorization"] = `Bearer ${process.env.REKAI_GATEWAY_KEY}`;
  }

  let res;
  try {
    res = await fetch(`${API_URL}/v1/embeddings`, {
      method: "POST",
      headers,
      body: JSON.stringify({ model: MODEL, input: inputs }),
    });
  } catch (err) {
    throw new Error(`Could not reach RekAI at ${API_URL}: ${err.message}`);
  }

  if (!res.ok) {
    throw new Error(`API error ${res.status}: ${await res.text()}`);
  }
  return res.json();
}

function cosine(a, b) {
  let dot = 0;
  let na = 0;
  let nb = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  return na && nb ? dot / (Math.sqrt(na) * Math.sqrt(nb)) : 0;
}

const texts =
  process.argv.length > 2
    ? process.argv.slice(2)
    : ["a cat sat on the mat", "a feline rested on the rug", "quarterly revenue grew"];

try {
  const result = await embed(texts);
  const vectors = result.embeddings;
  console.log(`provider=${result.provider} model=${result.model} dim=${vectors[0].length}`);
  for (let i = 1; i < vectors.length; i++) {
    const sim = cosine(vectors[0], vectors[i]).toFixed(4);
    console.log(`  sim(${JSON.stringify(texts[0])}, ${JSON.stringify(texts[i])}) = ${sim}`);
  }
} catch (err) {
  console.error(err.message);
  process.exit(1);
}
