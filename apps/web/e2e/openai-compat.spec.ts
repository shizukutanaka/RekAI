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

test("OpenAI-compatible /v1/chat/completions returns a chat.completion", async () => {
  const res = await fetch(`${API_URL}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "echo",
      messages: [{ role: "user", content: "ping" }],
    }),
  });
  expect(res.status).toBe(200);
  const body = await res.json();
  expect(body.object).toBe("chat.completion");
  expect(body.model).toBe("echo");
  expect(body.choices[0].message.role).toBe("assistant");
  expect(body.choices[0].message.content).toContain("Echo: ping");
  expect(body.choices[0].finish_reason).toBe("stop");
  expect(typeof body.usage.total_tokens).toBe("number");
});

test("OpenAI-compatible endpoint streams chat.completion.chunk frames", async () => {
  const res = await fetch(`${API_URL}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "echo",
      messages: [{ role: "user", content: "stream me" }],
      stream: true,
    }),
  });
  expect(res.status).toBe(200);
  expect(res.headers.get("content-type")).toContain("text/event-stream");

  const text = await res.text();
  // SSE frames: at least one chunk with the streaming object type, ending [DONE].
  expect(text).toContain('"object": "chat.completion.chunk"');
  expect(text.trimEnd().endsWith("data: [DONE]")).toBe(true);

  // Reassemble the streamed content deltas — echo should stream back the input.
  const deltas: string[] = [];
  for (const line of text.split("\n")) {
    if (!line.startsWith("data:")) continue;
    const payload = line.slice("data:".length).trim();
    if (payload === "[DONE]") break;
    const chunk = JSON.parse(payload);
    const piece = chunk.choices?.[0]?.delta?.content;
    if (piece) deltas.push(piece);
  }
  expect(deltas.join("")).toContain("Echo: stream me");
});

test("Settings page documents the OpenAI SDK drop-in base URL", async ({ page }) => {
  await page.goto("/settings");
  const section = page.locator(".field", {
    hasText: "Use it from the OpenAI SDK",
  });
  await expect(section).toBeVisible();
  // The snippet shows the drop-in base URL pointing at this deployment's /v1.
  await expect(section.locator("pre")).toContainText(`${API_URL}/v1`);
  await expect(section.locator("pre")).toContainText("client.chat.completions.create");
});

test("Settings page does not render the saved gateway key in the SDK example", async ({
  page,
}) => {
  await page.goto("/settings");
  await page.evaluate(() => {
    window.localStorage.setItem("rekai.gatewayKey", "sk-rekai-secret-should-stay-hidden");
  });
  await page.reload();

  const snippet = page.locator(".field", {
    hasText: "Use it from the OpenAI SDK",
  }).locator("pre");
  await expect(snippet).toContainText('api_key="sk-rekai-..."');
  await expect(snippet).not.toContainText("sk-rekai-secret-should-stay-hidden");
});

test("chat page keeps duplicate model ids provider-specific", async ({ page }) => {
  let requestedProvider: string | undefined;
  await page.route(`${API_URL}/v1/models`, async (route) => {
    await route.fulfill({
      json: {
        data: [
          { id: "shared-model", provider: "first", type: "chat" },
          { id: "shared-model", provider: "second", type: "chat" },
        ],
      },
    });
  });
  await page.route(`${API_URL}/v1/chat`, async (route) => {
    requestedProvider = route.request().postDataJSON().provider;
    await route.fulfill({
      json: {
        id: "chat-1",
        provider: "second",
        model: "shared-model",
        content: "ok",
        usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
        cost_usd: 0,
        cached: false,
        created: 0,
      },
    });
  });

  await page.goto("/");
  await page.locator("#model").selectOption({ index: 1 });
  await page.getByLabel("Stream").uncheck();
  await page.getByPlaceholder(/Type a message/).fill("ping");
  await page.getByRole("button", { name: "Send" }).click();

  await expect(page.locator(".msg.assistant").last()).toContainText("ok");
  expect(requestedProvider).toBe("second");
});
