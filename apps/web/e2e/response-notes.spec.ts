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
