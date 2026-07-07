import { expect, test } from "@playwright/test";

import { startApi, stopApi } from "./helpers/api-server";
import type { ChildProcess } from "node:child_process";

let api: ChildProcess;
const GATEWAY_KEY = "sk-e2e-gateway-key";

test.beforeAll(async () => {
  api = await startApi({ REKAI_API_KEYS: GATEWAY_KEY });
});

test.afterAll(async () => {
  await stopApi(api);
});

test("gateway auth: usage page 401s until a gateway key is saved, then works", async ({
  page,
}) => {
  // No key stored yet: /v1/usage 401s and the page shows the "unauthorized"
  // detail with a hint pointing at Settings.
  await page.goto("/usage");
  const error = page.locator(".error");
  await expect(error).toContainText("API key");
  await expect(error.locator("a")).toHaveAttribute("href", "/settings");

  // Save the gateway key in Settings.
  await page.goto("/settings");
  await page.fill("#gatewayKey", GATEWAY_KEY);
  await page.click('button:has-text("Save")');

  // Usage now loads real data instead of erroring.
  await page.goto("/usage");
  await expect(page.locator(".error")).toHaveCount(0);
  await expect(page.locator(".card-value").first()).toBeVisible();

  // Chat also works now that the gateway key is attached to /v1/* calls.
  await page.goto("/");
  await page.fill('textarea[placeholder*="Type a message"]', "hello with auth");
  await page.click('button:has-text("Send")');
  await expect(page.locator(".msg.assistant").last()).toContainText("Echo: hello with auth");
});
