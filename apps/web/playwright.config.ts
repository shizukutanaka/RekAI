import { defineConfig } from "@playwright/test";

// Fixed port, baked into the production build below as NEXT_PUBLIC_API_URL —
// see e2e/helpers/api-server.ts for why it must stay in sync.
const WEB_PORT = 3010;

export default defineConfig({
  testDir: "./e2e",
  // Each spec restarts the API with its own REKAI_* env on a fixed port
  // (helpers/api-server.ts), so specs can't run concurrently against it.
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  use: {
    baseURL: `http://localhost:${WEB_PORT}`,
    // Pre-installed Chromium in this environment; see CLAUDE.md / repo docs.
    // Only applies when PLAYWRIGHT_CHROMIUM_PATH is set (e.g. in this sandbox);
    // a normal local/CI checkout with `npx playwright install` needs neither.
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH }
      : {},
  },
  webServer: {
    command: `NEXT_PUBLIC_API_URL=http://localhost:8090 npm run build && npm run start -- -p ${WEB_PORT}`,
    url: `http://localhost:${WEB_PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
  },
});
