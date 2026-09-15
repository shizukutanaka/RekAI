import { expect, test } from "@playwright/test";

import { startApi, stopApi } from "./helpers/api-server";
import type { ChildProcess } from "node:child_process";

/**
 * A stream that fails partway through must not erase what already arrived.
 *
 * The API can emit deltas and then an `{"error": ...}` frame on the same
 * stream (an upstream 5xx surfacing after retries are exhausted, past the
 * point some text was already generated). `runChat`'s user-initiated stop
 * path already says the right thing in its own comment — "A user-initiated
 * stop is not an error — keep what streamed so far" — but a genuine upstream
 * error hits the same "the stream ended early" situation and was handled
 * differently: the outer `catch` filtered out *any* assistant message still
 * marked `streaming: true`, which is exactly the bubble holding the partial
 * text, deleting content the reader had already watched appear on screen.
 *
 * Responses are stubbed: what is under test is the UI's half of the contract.
 * That the API itself can emit deltas before an error frame is exercised by
 * `apps/api/tests/test_streaming.py`.
 */

let api: ChildProcess;

test.beforeAll(async () => {
  api = await startApi();
});

test.afterAll(async () => {
  await stopApi(api);
});

const PARTIAL = "The capital of France is";

test("partial text survives a mid-stream error, with an error shown", async ({ page }) => {
  await page.route("**/v1/chat/stream", async (route) => {
    const frames = [
      `data: ${JSON.stringify({ delta: PARTIAL })}\n\n`,
      `data: ${JSON.stringify({ error: "provider_error", detail: "Upstream failed mid-stream." })}\n\n`,
      "data: [DONE]\n\n",
    ].join("");
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: frames,
    });
  });

  await page.goto("/");
  await page.fill('textarea[placeholder*="Type a message"]', "hi");
  await page.click('button:has-text("Send")');

  const reply = page.locator(".msg.assistant").last();
  await expect(reply).toContainText(PARTIAL);
  await expect(page.locator(".error")).toContainText("Upstream failed mid-stream.");
});

test("an aborted stream (no error) keeps behaving the same way", async ({ page }) => {
  // The pre-existing case, kept alongside the one above so a fix can't satisfy
  // the new test by making every stream failure look like a user abort.
  await page.route("**/v1/chat/stream", async (route) => {
    // Never resolves within the test's lifetime; the Stop button aborts it.
    await new Promise(() => {});
  });

  await page.goto("/");
  await page.fill('textarea[placeholder*="Type a message"]', "hi");
  await page.click('button:has-text("Send")');
  await page.click('button:has-text("Stop")');

  await expect(page.locator(".error")).toHaveCount(0);
});
