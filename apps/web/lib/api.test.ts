import { describe, expect, it } from "vitest";
import { formatCost, parseSSEFrame } from "./api";

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
