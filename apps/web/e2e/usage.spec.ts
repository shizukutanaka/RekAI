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

test("usage dashboard renders the seeded counters and all breakdowns", async ({
  page,
}) => {
  // Seed two requests through the API the page polls (echo is keyless; the
  // second request carries `user` so the end-user section has a row too).
  for (const user of ["e2e-user-a", "e2e-user-b"]) {
    const res = await fetch(`${API_URL}/v1/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: "echo",
        messages: [{ role: "user", content: `seed ${user}` }],
        user,
      }),
    });
    expect(res.ok).toBe(true);
  }

  await page.goto("/usage");

  // Requests card reflects both seeded calls.
  await expect(
    page.locator(".card").filter({ hasText: "Requests" }).locator(".card-value"),
  ).toHaveText("2");

  // Per-provider and per-model sections both show the echo backend.
  await expect(page.locator(".bar-label", { hasText: "echo" }).first()).toBeVisible();
  await expect(page.getByText("Usage by model")).toBeVisible();
  await expect(page.getByText("Usage by client")).toBeVisible();

  // The end-user section lists both seeded `user` ids.
  await expect(page.getByText("Usage by end user")).toBeVisible();
  await expect(page.locator(".bar-label", { hasText: "e2e-user-a" })).toBeVisible();
  await expect(page.locator(".bar-label", { hasText: "e2e-user-b" })).toBeVisible();
});
