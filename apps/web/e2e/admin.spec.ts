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

  // Top up the runtime key -> the balance appears in the list and success message.
  await page.fill("#creditKey", "sk-e2e-added-runtime-key");
  await page.fill("#creditAmount", "5");
  await page.click('button:has-text("Grant")');
  await expect(dynamicList).toContainText("$5.00 credit");
  await expect(page.locator(".page")).toContainText("Granted $5");

  // A sub-cent top-up is visible even when the total balance exceeds one cent.
  await page.fill("#creditKey", "sk-e2e-added-runtime-key");
  await page.fill("#creditAmount", "0.004");
  await page.click('button:has-text("Grant")');
  await expect(dynamicList).toContainText("$5.004000 credit");

  // Switching to a wrong admin key clears the previous success message.
  await page.fill("#adminKey", "not-the-real-key");
  await page.click('button:has-text("Save & Refresh")');
  await expect(page.locator(".error")).toContainText("admin key");
  await expect(page.locator("p.saved")).toHaveCount(0);

  await page.fill("#adminKey", ADMIN_KEY);
  await page.click('button:has-text("Save & Refresh")');
  await expect(page.locator(".error")).toHaveCount(0);

  // Revoke it -> it disappears from the list again.
  await page.fill("#revokeKey", "sk-e2e-added-runtime-key");
  await page.click('button:has-text("Revoke")');
  await expect(page.getByText("None added yet.")).toBeVisible();
});
