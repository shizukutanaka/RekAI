import { expect, test } from "@playwright/test";

import { startApi, stopApi } from "./helpers/api-server";
import type { ChildProcess } from "node:child_process";

/**
 * A truncated answer must look truncated.
 *
 * The API reports the provider's real `finish_reason`, so `max_tokens` cutting a
 * reply short comes back as `"length"` rather than a synthesised `"stop"`. The
 * UI is where a reader actually finds out: on screen the text simply stops, and
 * a cut-off answer is indistinguishable from a complete one without a marker.
 *
 * The responses are stubbed rather than produced by a real backend: `echo` never
 * truncates, and what is under test is the UI's half of the contract — given an
 * API that says `length`, does the reader get told. That the API itself emits
 * `length` on both the streaming and non-streaming paths is covered by
 * `apps/api/tests/test_finish_reason.py`.
 */

let api: ChildProcess;

test.beforeAll(async () => {
  api = await startApi();
});

test.afterAll(async () => {
  await stopApi(api);
});

const CONTENT = "This answer was cut off";

test("a max_tokens truncation is called out on a streamed reply", async ({ page }) => {
  await page.route("**/v1/chat/stream", async (route) => {
    const frames = [
      `data: ${JSON.stringify({ delta: CONTENT })}\n\n`,
      `data: ${JSON.stringify({
        provider: "stub",
        model: "stub",
        usage: { prompt_tokens: 3, completion_tokens: 5, total_tokens: 8 },
        cost_usd: 0,
        estimated: false,
        finish_reason: "length",
      })}\n\n`,
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
  await expect(reply).toContainText(CONTENT);
  await expect(reply.locator(".meta")).toContainText("truncated");
});

test("a max_tokens truncation is called out on a non-streamed reply", async ({ page }) => {
  await page.route("**/v1/chat", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: "stub-1",
        provider: "stub",
        model: "stub",
        content: CONTENT,
        usage: { prompt_tokens: 3, completion_tokens: 5, total_tokens: 8 },
        cost_usd: 0,
        cached: false,
        created: 0,
        finish_reason: "length",
      }),
    });
  });

  await page.goto("/");
  await page.getByRole("checkbox").first().uncheck(); // Stream off
  await page.fill('textarea[placeholder*="Type a message"]', "hi");
  await page.click('button:has-text("Send")');

  const reply = page.locator(".msg.assistant").last();
  await expect(reply).toContainText(CONTENT);
  await expect(reply.locator(".meta")).toContainText("truncated");
});

test("an ordinary completion carries no truncation marker", async ({ page }) => {
  // The marker has to stay quiet on the common path, or it is noise that gets
  // ignored exactly when it matters.
  await page.goto("/");
  await page.fill('textarea[placeholder*="Type a message"]', "hello");
  await page.click('button:has-text("Send")');

  const reply = page.locator(".msg.assistant").last();
  await expect(reply).toContainText("Echo: hello");
  await expect(reply.locator(".meta")).not.toContainText("truncated");
});

test("a streamed reply is labelled with the provider that served it", async ({ page }) => {
  // The same slot the non-streaming path fills with the response's `provider`.
  // It used to be filled with the *requested model* instead, so the one label
  // meant two different things depending on a toggle — and a model-name-routed
  // request never showed which provider actually answered.
  await page.route("**/v1/chat/stream", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body:
        `data: ${JSON.stringify({ delta: "hi" })}\n\n` +
        `data: ${JSON.stringify({
          provider: "anthropic",
          model: "claude-x",
          usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
          cost_usd: 0,
          estimated: false,
        })}\n\n` +
        "data: [DONE]\n\n",
    });
  });

  await page.goto("/");
  await page.fill('textarea[placeholder*="Type a message"]', "hi");
  await page.click('button:has-text("Send")');

  await expect(page.locator(".msg.assistant").last().locator(".meta")).toContainText("anthropic");
});
