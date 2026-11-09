import { expect, test } from "@playwright/test";

import { API_URL, startApi, stopApi } from "./helpers/api-server";
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

test("embeddings page keeps duplicate model ids provider-specific", async ({ page }) => {
  let requestedProvider: string | undefined;
  await page.route(`${API_URL}/v1/models?type=embedding`, async (route) => {
    await route.fulfill({
      json: {
        data: [
          { id: "shared-embedding", provider: "first", type: "embedding" },
          { id: "shared-embedding", provider: "second", type: "embedding" },
        ],
      },
    });
  });
  await page.route(`${API_URL}/v1/embeddings`, async (route) => {
    requestedProvider = route.request().postDataJSON().provider;
    await route.fulfill({
      json: {
        provider: "second",
        model: "shared-embedding",
        embeddings: [[1, 0], [0.8, 0.2]],
        usage: { prompt_tokens: 2, completion_tokens: 0, total_tokens: 2 },
        cost_usd: 0,
        cached: false,
      },
    });
  });

  await page.goto("/embeddings");
  await page.locator("#model").selectOption({ index: 1 });
  await page.locator("#text").fill("one\ntwo");
  await page.getByRole("button", { name: "Embed" }).click();

  await expect(page.locator(".card", { hasText: "Provider" })).toContainText("second");
  expect(requestedProvider).toBe("second");
});
