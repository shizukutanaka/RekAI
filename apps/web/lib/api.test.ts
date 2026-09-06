import { describe, expect, it } from "vitest";
import {
  ApiError,
  cacheHitDetail,
  cacheNote,
  cooldownRemaining,
  cosineSimilarity,
  errorFromResponse,
  finishNote,
  formatCost,
  gatewayAuthHeaders,
  modelsOfType,
  parseRateLimit,
  parseSSEFrame,
  redactionNote,
} from "./api";

describe("formatCost", () => {
  it("returns empty string for null/undefined (unknown price)", () => {
    expect(formatCost(null)).toBe("");
    expect(formatCost(undefined)).toBe("");
  });

  it("shows 'free' for zero cost", () => {
    expect(formatCost(0)).toBe("free");
  });

  it("uses 4 decimals for sub-cent costs", () => {
    expect(formatCost(0.0042)).toBe("$0.0042");
  });

  it("uses 2 decimals for larger costs", () => {
    expect(formatCost(1.5)).toBe("$1.50");
    expect(formatCost(0.25)).toBe("$0.25");
  });
});

describe("cosineSimilarity", () => {
  it("is 1 for identical vectors", () => {
    expect(cosineSimilarity([1, 2, 3], [1, 2, 3])).toBeCloseTo(1, 6);
  });

  it("is 0 for orthogonal vectors", () => {
    expect(cosineSimilarity([1, 0], [0, 1])).toBeCloseTo(0, 6);
  });

  it("is -1 for opposite vectors", () => {
    expect(cosineSimilarity([1, 1], [-1, -1])).toBeCloseTo(-1, 6);
  });

  it("returns 0 for mismatched lengths, empty, or zero vectors", () => {
    expect(cosineSimilarity([1, 2], [1])).toBe(0);
    expect(cosineSimilarity([], [])).toBe(0);
    expect(cosineSimilarity([0, 0], [1, 1])).toBe(0);
  });
});

describe("modelsOfType", () => {
  const models = [
    { id: "gpt-4o", provider: "openai", type: "chat" as const },
    { id: "text-embedding-3-small", provider: "openai", type: "embedding" as const },
    { id: "legacy", provider: "echo" }, // no type -> treated as chat
  ];

  it("filters to embedding models", () => {
    expect(modelsOfType(models, "embedding").map((m) => m.id)).toEqual([
      "text-embedding-3-small",
    ]);
  });

  it("treats a missing type as chat", () => {
    expect(modelsOfType(models, "chat").map((m) => m.id)).toEqual(["gpt-4o", "legacy"]);
  });
});

describe("gatewayAuthHeaders", () => {
  it("returns an empty object when no key is set", () => {
    expect(gatewayAuthHeaders()).toEqual({});
    expect(gatewayAuthHeaders("")).toEqual({});
  });

  it("returns a Bearer Authorization header when a key is set", () => {
    expect(gatewayAuthHeaders("sk-rekai-abc")).toEqual({
      Authorization: "Bearer sk-rekai-abc",
    });
  });
});

describe("parseRateLimit", () => {
  it("reads the X-RateLimit-* headers", () => {
    const res = new Response("", {
      headers: { "X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "42" },
    });
    expect(parseRateLimit(res)).toEqual({ limit: 60, remaining: 42 });
  });

  it("returns nulls when the headers are absent", () => {
    expect(parseRateLimit(new Response(""))).toEqual({ limit: null, remaining: null });
  });

  it("returns null for a non-numeric header", () => {
    const res = new Response("", { headers: { "X-RateLimit-Remaining": "n/a" } });
    expect(parseRateLimit(res).remaining).toBeNull();
  });
});

describe("errorFromResponse", () => {
  it("uses Retry-After for a 429", async () => {
    const res = new Response(JSON.stringify({ detail: "Too many requests." }), {
      status: 429,
      headers: { "Retry-After": "30" },
    });
    const err = await errorFromResponse(res);
    expect(err.message).toBe("Rate limited — retry in 30s.");
  });

  it("falls back for a 429 with no header", async () => {
    const res = new Response("nope", { status: 429 });
    expect((await errorFromResponse(res)).message).toBe("Rate limited — slow down.");
  });

  it("uses the body detail for other errors", async () => {
    const res = new Response(JSON.stringify({ detail: "no key" }), { status: 401 });
    expect((await errorFromResponse(res)).message).toBe("no key");
  });

  it("falls back to the status when the body is not JSON", async () => {
    const res = new Response("boom", { status: 500 });
    expect((await errorFromResponse(res)).message).toBe("Request failed (500)");
  });

  it("carries the HTTP status so callers can branch on the code", async () => {
    // The admin page distinguishes a 404 ("admin API not configured") from a
    // 401 by inspecting `.status`, not the server-controlled message string.
    const res = new Response(JSON.stringify({ detail: "Not Found" }), { status: 404 });
    const err = await errorFromResponse(res);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(404);
  });
});

describe("parseSSEFrame", () => {
  it("parses a delta event", () => {
    expect(parseSSEFrame('data: {"delta": "Hello"}')).toEqual({
      kind: "delta",
      text: "Hello",
    });
  });

  it("recognizes the terminator", () => {
    expect(parseSSEFrame("data: [DONE]")).toEqual({ kind: "done" });
  });

  it("surfaces error events with detail", () => {
    expect(
      parseSSEFrame('data: {"error": "provider_error", "detail": "no key"}'),
    ).toEqual({ kind: "error", message: "no key" });
  });

  it("falls back to the error code when detail is absent", () => {
    expect(parseSSEFrame('data: {"error": "boom"}')).toEqual({
      kind: "error",
      message: "boom",
    });
  });

  it("parses a usage summary event", () => {
    const ev = parseSSEFrame(
      'data: {"provider":"echo","model":"echo","usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3},"cost_usd":0,"estimated":true}',
    );
    expect(ev.kind).toBe("summary");
    if (ev.kind === "summary") {
      expect(ev.summary.usage.total_tokens).toBe(3);
      expect(ev.summary.estimated).toBe(true);
    }
  });

  it("ignores frames without a data line", () => {
    expect(parseSSEFrame(": comment")).toEqual({ kind: "ignore" });
    expect(parseSSEFrame("event: ping")).toEqual({ kind: "ignore" });
  });

  it("ignores malformed JSON", () => {
    expect(parseSSEFrame("data: not json")).toEqual({ kind: "ignore" });
  });

  it("reads the data line out of a multi-line frame", () => {
    expect(parseSSEFrame('event: message\ndata: {"delta": "x"}')).toEqual({
      kind: "delta",
      text: "x",
    });
  });
});

describe("finishNote", () => {
  // The API reports the provider's real finish_reason so a truncated answer is
  // distinguishable from a complete one. The UI is where a reader actually finds
  // out: without this the text just stops, and looks finished.
  it("says nothing for an answer that simply ended", () => {
    expect(finishNote("stop")).toBe("");
    expect(finishNote("tool_calls")).toBe("");
  });

  it("says nothing when the provider did not report a reason", () => {
    expect(finishNote(null)).toBe("");
    expect(finishNote(undefined)).toBe("");
  });

  it("flags a max-tokens truncation, and says what to change", () => {
    const note = finishNote("length");
    expect(note).not.toBe("");
    expect(note).toContain("truncated");
    expect(note).toContain("max tokens");
  });

  it("flags a provider content filter", () => {
    expect(finishNote("content_filter")).toContain("content filter");
  });
});

describe("cacheNote", () => {
  it("says nothing for a freshly generated answer", () => {
    expect(cacheNote(false, null)).toBe("");
    expect(cacheNote(undefined, undefined)).toBe("");
  });

  it("reports a plain cache hit when the prompt matched exactly", () => {
    expect(cacheNote(true, null)).toBe("cached ⚡");
    expect(cacheNote(true, undefined)).toBe("cached ⚡");
  });

  it("says the answer belongs to a different prompt on a semantic hit", () => {
    // The API returns cache_similarity only for a semantic hit, and that is the
    // whole point: the reader is looking at the answer to a *neighbouring*
    // question. Rendering it as a bare "cached" claims otherwise.
    const note = cacheNote(true, 0.9123);
    expect(note).toContain("91%");
    expect(note).toContain("similar prompt");
  });
});

describe("redactionNote", () => {
  it("says nothing when the answer was untouched", () => {
    expect(redactionNote(null)).toBe("");
    expect(redactionNote(undefined)).toBe("");
    expect(redactionNote([])).toBe("");
  });

  it("reports how many secrets were scrubbed, with correct plurals", () => {
    // The text on screen is not what the model produced; a redaction leaves a
    // placeholder that reads like ordinary content.
    expect(redactionNote(["aws_key"])).toBe("1 secret redacted");
    expect(redactionNote(["aws_key", "github_pat"])).toBe("2 secrets redacted");
  });
});

describe("cacheHitDetail", () => {
  it("is just the ratio when nothing was served semantically", () => {
    expect(cacheHitDetail(12, 30, 0)).toBe("12 / 30");
    expect(cacheHitDetail(12, 30, undefined)).toBe("12 / 30");
  });

  it("breaks out semantic hits, which are approximate matches", () => {
    // semantic_cache_hits_total is a *subset* of cache_hits_total, so the tile's
    // headline rate silently includes answers to prompts nobody asked. That is
    // the number an operator uses to decide the cache is earning its keep.
    expect(cacheHitDetail(12, 30, 5)).toBe("12 / 30 · 5 semantic");
  });
});

describe("cooldownRemaining", () => {
  it("says nothing for a provider that is not parked", () => {
    expect(cooldownRemaining(undefined)).toBe("");
    expect(cooldownRemaining(0)).toBe("");
    expect(cooldownRemaining(-1)).toBe("");
  });

  it("rounds up to whole seconds under a minute", () => {
    expect(cooldownRemaining(0.4)).toBe("≈1s");
    expect(cooldownRemaining(26.2)).toBe("≈27s");
    expect(cooldownRemaining(59)).toBe("≈59s");
  });

  it("switches to minutes at a minute", () => {
    expect(cooldownRemaining(60)).toBe("≈1m");
    expect(cooldownRemaining(61)).toBe("≈2m");
  });
});
