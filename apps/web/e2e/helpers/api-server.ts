import { ChildProcess, spawn } from "node:child_process";
import path from "node:path";

// All specs share one fixed API port (must match NEXT_PUBLIC_API_URL baked
// into the web build by playwright.config.ts's webServer). Specs run serially
// (playwright.config.ts sets workers: 1) since each one restarts the API with
// its own REKAI_* env — they can't run concurrently on the same port.
export const API_PORT = 8090;
export const API_URL = `http://localhost:${API_PORT}`;

const UVICORN = path.resolve(__dirname, "../../../api/.venv/bin/uvicorn");
const API_DIR = path.resolve(__dirname, "../../../api");

/** Start the API with the given REKAI_* env overrides; resolves once /health responds. */
export async function startApi(env: Record<string, string> = {}): Promise<ChildProcess> {
  const proc = spawn(UVICORN, ["rekai.main:app", "--port", String(API_PORT)], {
    cwd: API_DIR,
    env: {
      ...process.env,
      REKAI_ENVIRONMENT: "test",
      REKAI_RATE_LIMIT_ENABLED: "false",
      REKAI_DEFAULT_PROVIDER: "echo",
      ...env,
    },
    stdio: "ignore",
  });

  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${API_URL}/health`);
      if (res.ok) return proc;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  proc.kill("SIGKILL");
  throw new Error(`API did not become healthy on ${API_URL} within 15s`);
}

export function stopApi(proc: ChildProcess): Promise<void> {
  return new Promise((resolve) => {
    if (proc.exitCode !== null || proc.killed) {
      resolve();
      return;
    }
    proc.once("exit", () => resolve());
    proc.kill("SIGKILL");
  });
}
