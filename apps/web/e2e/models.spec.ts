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

test("models page groups the catalog by provider with type + price", async ({ page }) => {
  await page.goto("/models");

  const echoSection = page.locator("h3.section", { hasText: "echo" });
  await expect(echoSection).toBeVisible();

  const rows = page.locator(".providers li");
  await expect(rows).not.toHaveCount(0);
  await expect(page.locator(".providers li", { hasText: "gpt-4o" }).first()).toContainText("chat");
  await expect(
    page.locator(".providers li", { hasText: "text-embedding-3-small" }).first(),
  ).toContainText("embedding");
});

test("type filter narrows the catalog to one kind", async ({ page }) => {
  await page.goto("/models");
  await expect(page.locator(".providers li").first()).toBeVisible();

  await page.click('button:has-text("Embedding")');
  for (const row of await page.locator(".providers li").all()) {
    await expect(row).toContainText("embedding");
  }
  await expect(page.locator(".providers li", { hasText: "gpt-4o" })).toHaveCount(0);

  await page.click('button:has-text("All")');
  await expect(page.locator(".providers li", { hasText: "gpt-4o" }).first()).toBeVisible();
});
