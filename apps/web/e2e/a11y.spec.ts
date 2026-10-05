import { AxeBuilder } from "@axe-core/playwright";
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

// Every navigable page is covered — a scan that skips one route wouldn't catch
// a regression that lives only there.
const PAGES = ["/", "/models", "/embeddings", "/usage", "/admin", "/settings"];

test("every page has a non-empty accessibility tree", async ({ page }) => {
  for (const path of PAGES) {
    await page.goto(path);
    // Content must exist before a scan means anything — an error page is
    // perfectly accessible and perfectly useless.
    await expect(page.locator(".nav").first()).toBeVisible();
    await expect(page.locator(".shell").first()).toBeVisible();
  }
});

test("no serious WCAG violations on any page", async ({ page }) => {
  const findings: string[] = [];
  for (const path of PAGES) {
    await page.goto(path);
    await expect(page.locator(".nav").first()).toBeVisible();
    const results = await new AxeBuilder({ page }).analyze();
    for (const v of results.violations) {
      if (v.impact === "critical" || v.impact === "serious") {
        findings.push(`${path} [${v.impact}] ${v.id}: ${v.nodes.length} node(s)`);
      }
    }
  }
  expect(findings).toEqual([]);
});
