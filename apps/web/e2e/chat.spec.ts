import { expect, test } from "@playwright/test";

import { startApi, stopApi } from "./helpers/api-server";
import type { ChildProcess } from "node:child_process";

let api: ChildProcess;

test.beforeAll(async () => {
  api = await startApi();
});

test.afterAll(async () => {
  await stopApi(api);
});

test("sending a message to the echo model renders a reply with metadata", async ({ page }) => {
  await page.goto("/");
  await page.fill('textarea[placeholder*="Type a message"]', "hello e2e");
  await page.click('button:has-text("Send")');

  const reply = page.locator(".msg.assistant").last();
  await expect(reply).toContainText("Echo: hello e2e");
  await expect(reply).toContainText("echo");
  await expect(reply).toContainText("tokens");
});
