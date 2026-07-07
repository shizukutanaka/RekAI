import { expect, test } from "@playwright/test";

import { startApi, stopApi } from "./helpers/api-server";
import type { ChildProcess } from "node:child_process";

let api: ChildProcess;
const ADMIN_KEY = "sk-e2e-admin-key";

test.beforeAll(async () => {
  api = await startApi({
    REKAI_ADMIN_KEY: ADMIN_KEY,
    REKAI_DYNAMIC_KEYS_ENABLED: "true",
  });
});

test.afterAll(async () => {
  await stopApi(api);
});

test("admin page: wrong key errors, correct key manages runtime keys", async ({ page }) => {
  await page.goto("/admin");

  // Wrong admin key -> the 401 surfaces as an error, not a silent failure.
  await page.fill("#adminKey", "not-the-real-key");
  await page.click('button:has-text("Save & Refresh")');
  await expect(page.locator(".error")).toContainText("admin key");

  // Correct admin key -> the (empty) key lists load.
  await page.fill("#adminKey", ADMIN_KEY);
  await page.click('button:has-text("Save & Refresh")');
  await expect(page.locator(".error")).toHaveCount(0);
  await expect(page.getByText("None added yet.")).toBeVisible();

  // Add a runtime key -> it appears, masked, in the dynamic key list.
  await page.fill("#newKey", "sk-e2e-added-runtime-key");
  await page.click('button:has-text("Add")');
  const dynamicList = page.locator("ul.providers").last();
  await expect(dynamicList).toContainText("sk-e…-key");
  await expect(page.locator(".page")).not.toContainText("sk-e2e-added-runtime-key");

  // Revoke it -> it disappears from the list again.
  await page.fill("#revokeKey", "sk-e2e-added-runtime-key");
  await page.click('button:has-text("Revoke")');
  await expect(page.getByText("None added yet.")).toBeVisible();
});
