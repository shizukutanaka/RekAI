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

test("checking text against the echo provider renders an ok badge", async ({ page }) => {
  await page.goto("/moderations");
  await page.fill("#provider", "echo");
  await page.fill("#text", "a harmless line");
  await page.click('button:has-text("Check")');

  await expect(page.locator(".badge").first()).toHaveText("ok");
  await expect(page.locator(".card", { hasText: "Provider" })).toContainText("echo");
  await expect(page.locator(".card", { hasText: "Flagged" })).toContainText("0");
});
