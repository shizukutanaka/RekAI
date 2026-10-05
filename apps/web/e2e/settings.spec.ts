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

test("settings: save persists keys to localStorage and restores them on revisit", async ({
  page,
}) => {
  await page.goto("/settings");

  await page.fill("#key", "sk-e2e-provider-key");
  await page.fill("#gatewayKey", "sk-e2e-gateway-key");
  await page.click('button:has-text("Save")');
  await expect(page.getByText("Saved ✓")).toBeVisible();

  await expect
    .poll(() =>
      page.evaluate(() => [
        window.localStorage.getItem("rekai.providerKey"),
        window.localStorage.getItem("rekai.gatewayKey"),
      ]),
    )
    .toEqual(["sk-e2e-provider-key", "sk-e2e-gateway-key"]);

  // Revisiting (or reloading) restores both fields from storage.
  await page.goto("/");
  await page.goto("/settings");
  await expect(page.locator("#key")).toHaveValue("sk-e2e-provider-key");
  await expect(page.locator("#gatewayKey")).toHaveValue("sk-e2e-gateway-key");
});

test("settings: Enter inside a password field submits the form", async ({ page }) => {
  await page.goto("/settings");

  await page.fill("#key", "sk-e2e-enter-key");
  // The whole point of the real <form>: password managers and Enter both work.
  await page.locator("#key").press("Enter");
  await expect(page.getByText("Saved ✓")).toBeVisible();
  await expect
    .poll(() => page.evaluate(() => window.localStorage.getItem("rekai.providerKey")))
    .toBe("sk-e2e-enter-key");
});

test("settings: provider readiness renders ready vs needs-key badges", async ({ page }) => {
  await page.goto("/settings");

  // The health block lists every registered provider; echo is keyless
  // (ready), the rest need BYOK (needs key).
  const list = page.locator("ul.providers");
  await expect(list).toBeVisible();
  await expect(list.locator("li")).not.toHaveCount(0);
  await expect(list).toContainText("echo");
  await expect(list.locator(".badge.ready").first()).toBeVisible();
  await expect(list.locator(".badge:not(.ready)").first()).toBeVisible();
});
