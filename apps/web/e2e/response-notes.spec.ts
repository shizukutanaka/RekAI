import { expect, test } from "@playwright/test";

import { startApi, stopApi } from "./helpers/api-server";
import type { ChildProcess } from "node:child_process";

/**
 * Two more things the API reports about an answer that the reader cannot see
 * from the text alone.
 *
 * `cache_similarity` marks a *semantic* cache hit: the reply is the stored
 * answer to a different, similar prompt. Rendering that as a bare "cached"
 * tells the reader their question was answered when a neighbouring one was.
 * Confirmed live against the real API with the semantic cache on — asking "what
 * is the capital city of France" returned the stored answer to "what is the
 * capital of France" with `cached: true, cache_similarity: 0.7458`.
 *
 * `redacted` names the secret patterns the output guardrail scrubbed. A
 * redaction leaves a placeholder that reads like ordinary content, so without a
 * marker the reader has no way to know the text was altered.
 *
 * Responses are stubbed: what is under test is the UI's half of the contract.
 */

let api: ChildProcess;

test.beforeAll(async () => {
  api = await startApi();
});

test.afterAll(async () => {
  await stopApi(api);
});

async function stubChat(page: import("@playwright/test").Page, extra: object) {
  await page.route("**/v1/chat", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "stub-1",
        provider: "stub",
        model: "stub",
        content: "Paris",
        usage: { prompt_tokens: 3, completion_tokens: 1, total_tokens: 4 },
        cost_usd: 0,
        cached: false,
        created: 0,
        ...extra,
      }),
    });
  });
  await page.goto("/");
  await page.getByRole("checkbox").first().uncheck(); // Stream off
  await page.fill('textarea[placeholder*="Type a message"]', "hi");
  await page.click('button:has-text("Send")');
  return page.locator(".msg.assistant").last().locator(".meta");
}

test("a semantic cache hit says the answer belongs to a different prompt", async ({ page }) => {
  const meta = await stubChat(page, { cached: true, cache_similarity: 0.7458 });

  await expect(meta).toContainText("75%");
  await expect(meta).toContainText("similar prompt");
});

test("an exact cache hit is still just 'cached'", async ({ page }) => {
  const meta = await stubChat(page, { cached: true, cache_similarity: null });

  await expect(meta).toContainText("cached");
  await expect(meta).not.toContainText("similar prompt");
});

test("a redacted answer says so", async ({ page }) => {
  const meta = await stubChat(page, { redacted: ["aws_access_key", "github_pat"] });

  await expect(meta).toContainText("2 secrets redacted");
});

test("an untouched answer carries neither marker", async ({ page }) => {
  const meta = await stubChat(page, {});

  await expect(meta).not.toContainText("similar prompt");
  await expect(meta).not.toContainText("redacted");
});

test("the usage dashboard breaks out semantic cache hits", async ({ page }) => {
  // semantic_cache_hits_total is a subset of cache_hits_total, so the headline
  // hit rate silently counts approximate matches as cache wins. Observed live
  // with the semantic cache on: cache_hits_total 1, semantic_cache_hits_total 1
  // — every "hit" was an answer to a different prompt, shown as a plain rate.
  await page.route("**/v1/usage", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        requests_total: 30,
        cache_hits_total: 12,
        cache_misses_total: 18,
        semantic_cache_hits_total: 5,
        errors_total: 0,
        fallbacks_total: 0,
        retries_total: 0,
        cooldowns_total: 0,
        tokens_total: 100,
        cost_usd_total: 0,
        requests_by_provider: {},
        usage_by_client: {},
      }),
    });
  });

  await page.goto("/usage");

  await expect(page.locator("body")).toContainText("12 / 30 · 5 semantic");
});

test("a parked provider is called out before you send", async ({ page }) => {
  // /health reports parked_providers (provider -> seconds of cooldown left).
  // It is why a reply arrives from somewhere other than the provider selected —
  // RekAI skips a cooling-down provider in favour of a healthy fallback — and
  // the UI dropped the field entirely, leaving the reroute looking arbitrary.
  await page.route("**/health", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "degraded",
        version: "test",
        providers: ["echo"],
        provider_status: { echo: "ready" },
        parked_providers: { echo: 26.2 },
        cache: "memory",
      }),
    });
  });

  await page.goto("/");

  const notice = page.locator(".notice");
  await expect(notice).toContainText("cooling down");
  await expect(notice).toContainText("≈27s");
});

test("no cooldown notice when nothing is parked", async ({ page }) => {
  // The real API, with nothing parked: the notice element is not rendered at
  // all, so this asserts on the page rather than on a locator that has no match.
  await page.goto("/");
  await expect(page.locator(".msg, textarea").first()).toBeVisible();

  await expect(page.locator("body")).not.toContainText("cooling down");
});
