export const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

export interface ChatResponse {
  id: string;
  provider: string;
  model: string;
  content: string;
  usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number };
  cost_usd: number | null;
  cached: boolean;
  created: number;
}

/** Format an estimated USD cost for display, or "" when unknown. */
export function formatCost(cost: number | null | undefined): string {
  if (cost === null || cost === undefined) return "";
  if (cost === 0) return "free";
  if (cost < 0.01) return `$${cost.toFixed(4)}`;
  return `$${cost.toFixed(2)}`;
}

export interface ModelInfo {
  id: string;
  provider: string;
}

export interface UsageSummary {
  requests_total: number;
  cache_hits_total: number;
  cache_misses_total: number;
  errors_total: number;
  fallbacks_total: number;
  tokens_total: number;
  cost_usd_total: number;
  requests_by_provider: Record<string, number>;
}

const KEY_STORAGE = "rekai.providerKey";

export function getStoredKey(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(KEY_STORAGE) || "";
}

export function setStoredKey(value: string): void {
  if (typeof window === "undefined") return;
  if (value) {
    window.localStorage.setItem(KEY_STORAGE, value);
  } else {
    window.localStorage.removeItem(KEY_STORAGE);
  }
}

export async function sendChat(params: {
  model: string;
  messages: ChatMessage[];
  providerKey?: string;
  temperature?: number;
}): Promise<ChatResponse> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (params.providerKey) headers["X-Provider-Key"] = params.providerKey;

  const res = await fetch(`${API_URL}/v1/chat`, {
    method: "POST",
    headers,
    body: JSON.stringify({
      model: params.model,
      messages: params.messages,
      ...(params.temperature != null ? { temperature: params.temperature } : {}),
    }),
  });

  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      detail = body.detail || body.error || detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return res.json();
}

/**
 * Stream a chat completion. Calls `onDelta` for each incremental text chunk.
 * Resolves when the stream completes; rejects on transport or provider errors.
 */
export async function streamChat(
  params: {
    model: string;
    messages: ChatMessage[];
    providerKey?: string;
    temperature?: number;
  },
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (params.providerKey) headers["X-Provider-Key"] = params.providerKey;

  const res = await fetch(`${API_URL}/v1/chat/stream`, {
    method: "POST",
    headers,
    body: JSON.stringify({
      model: params.model,
      messages: params.messages,
      ...(params.temperature != null ? { temperature: params.temperature } : {}),
    }),
    signal,
  });

  if (!res.ok || !res.body) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      detail = body.detail || body.error || detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  // SSE frames are separated by a blank line; each carries one `data:` payload.
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const ev = parseSSEFrame(frame);
      if (ev.kind === "done") return;
      if (ev.kind === "delta") onDelta(ev.text);
      else if (ev.kind === "error") throw new Error(ev.message);
    }
  }
}

export type SSEEvent =
  | { kind: "delta"; text: string }
  | { kind: "done" }
  | { kind: "error"; message: string }
  | { kind: "ignore" };

/**
 * Parse a single SSE frame (text between blank-line separators) into a typed
 * event. Pure and side-effect free so it can be unit-tested.
 */
export function parseSSEFrame(frame: string): SSEEvent {
  const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
  if (!dataLine) return { kind: "ignore" };
  const payload = dataLine.slice("data:".length).trim();
  if (payload === "[DONE]") return { kind: "done" };
  try {
    const event = JSON.parse(payload);
    if (event.delta) return { kind: "delta", text: event.delta };
    if (event.error) return { kind: "error", message: event.detail || event.error };
    return { kind: "ignore" };
  } catch {
    return { kind: "ignore" };
  }
}

export async function fetchModels(): Promise<ModelInfo[]> {
  try {
    const res = await fetch(`${API_URL}/v1/models`);
    if (!res.ok) return [];
    const body = await res.json();
    return body.data ?? [];
  } catch {
    return [];
  }
}

export async function fetchUsage(): Promise<UsageSummary> {
  const res = await fetch(`${API_URL}/v1/usage`, { cache: "no-store" });
  if (!res.ok) throw new Error(`Could not load usage (${res.status})`);
  return res.json();
}
