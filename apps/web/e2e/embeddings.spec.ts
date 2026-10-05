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

test("embedding the sample lines returns vectors, dims, and pairwise similarity", async ({
  page,
}) => {
  await page.goto("/embeddings");

  // The prefilled sample has three non-empty lines; echo needs no key.
  await page.click('button:has-text("Embed")');

  const cards = page.locator(".card");
  await expect(cards.filter({ hasText: "Vectors" }).locator(".card-value")).toHaveText("3");
  await expect(cards.filter({ hasText: "Dimensions" }).locator(".card-value")).toHaveText("16");
  await expect(cards.filter({ hasText: "Provider" }).locator(".card-value")).toHaveText("echo");

  // 3 inputs → 3 pairs, each scored 0-1.
  await expect(page.locator(".bar-row")).toHaveCount(3);
  await expect(page.locator(".bar-count").first()).toHaveText(/0\.\d{3}/);
});
