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
  cached: boolean;
  created: number;
}

export interface ModelInfo {
  id: string;
  provider: string;
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
}): Promise<ChatResponse> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (params.providerKey) headers["X-Provider-Key"] = params.providerKey;

  const res = await fetch(`${API_URL}/v1/chat`, {
    method: "POST",
    headers,
    body: JSON.stringify({ model: params.model, messages: params.messages }),
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
  params: { model: string; messages: ChatMessage[]; providerKey?: string },
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (params.providerKey) headers["X-Provider-Key"] = params.providerKey;

  const res = await fetch(`${API_URL}/v1/chat/stream`, {
    method: "POST",
    headers,
    body: JSON.stringify({ model: params.model, messages: params.messages }),
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
      const dataLine = frame
        .split("\n")
        .find((l) => l.startsWith("data:"));
      if (!dataLine) continue;
      const payload = dataLine.slice("data:".length).trim();
      if (payload === "[DONE]") return;
      try {
        const event = JSON.parse(payload);
        if (event.delta) onDelta(event.delta);
        else if (event.error) throw new Error(event.detail || event.error);
      } catch (err) {
        if (err instanceof Error && err.message) throw err;
        /* ignore malformed frame */
      }
    }
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
