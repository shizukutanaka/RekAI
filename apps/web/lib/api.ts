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
