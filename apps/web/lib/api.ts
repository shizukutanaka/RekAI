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

export interface RateLimitInfo {
  limit: number | null;
  remaining: number | null;
}

/** Read the `X-RateLimit-*` headers off a response (null when absent). */
export function parseRateLimit(res: Response): RateLimitInfo {
  const num = (h: string): number | null => {
    const v = res.headers.get(h);
    if (v == null || v === "") return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };
  return { limit: num("X-RateLimit-Limit"), remaining: num("X-RateLimit-Remaining") };
}

/**
 * An Error that carries the HTTP status of the failed response, so callers can
 * branch on the code (e.g. 404 vs 401) instead of matching the message string.
 */
export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * Build a user-friendly ApiError from a failed response. A 429 yields a clear
 * "rate limited — retry in Ns" message sourced from the `Retry-After` header;
 * otherwise the API's `detail`/`error` body is used. The `.status` field lets
 * callers distinguish, e.g., a 404 ("admin API not configured") from a 401.
 */
export async function errorFromResponse(res: Response): Promise<ApiError> {
  if (res.status === 429) {
    const retry = res.headers.get("Retry-After");
    return new ApiError(
      retry ? `Rate limited — retry in ${retry}s.` : "Rate limited — slow down.",
      res.status,
    );
  }
  let detail = `Request failed (${res.status})`;
  try {
    const body = await res.json();
    detail = body.detail || body.error || detail;
  } catch {
    /* ignore */
  }
  return new ApiError(detail, res.status);
}

export interface ModelPricing {
  input_per_1m: number;
  output_per_1m: number;
}

export interface ModelInfo {
  id: string;
  provider: string;
  type?: "chat" | "embedding";
  pricing?: ModelPricing | null;
}

/** Filter models by type, treating a missing `type` as "chat" (older APIs). */
export function modelsOfType(
  models: ModelInfo[],
  type: "chat" | "embedding",
): ModelInfo[] {
  return models.filter((m) => (m.type ?? "chat") === type);
}

export interface HealthResponse {
  status: string;
  version: string;
  providers: string[];
  provider_status: Record<string, "ready" | "byok_only">;
  cache: string;
}

export interface ClientUsage {
  requests: number;
  tokens: number;
  cost_usd: number;
}

export interface UsageSummary {
  requests_total: number;
  cache_hits_total: number;
  cache_misses_total: number;
  errors_total: number;
  fallbacks_total: number;
  retries_total: number;
  cooldowns_total: number;
  tokens_total: number;
  cost_usd_total: number;
  requests_by_provider: Record<string, number>;
  usage_by_client: Record<string, ClientUsage>;
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

// Distinct from the BYOK provider key above: this is the *gateway's own*
// REKAI_API_KEYS credential, sent as `Authorization: Bearer <key>` so the web
// app keeps working once an operator turns on gateway auth.
const GATEWAY_KEY_STORAGE = "rekai.gatewayKey";

export function getStoredGatewayKey(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(GATEWAY_KEY_STORAGE) || "";
}

export function setStoredGatewayKey(value: string): void {
  if (typeof window === "undefined") return;
  if (value) {
    window.localStorage.setItem(GATEWAY_KEY_STORAGE, value);
  } else {
    window.localStorage.removeItem(GATEWAY_KEY_STORAGE);
  }
}

/** Build the `Authorization` header for a gateway key, or {} when unset. */
export function gatewayAuthHeaders(gatewayKey?: string): Record<string, string> {
  return gatewayKey ? { Authorization: `Bearer ${gatewayKey}` } : {};
}

// A third, distinct credential: REKAI_ADMIN_KEY, for the /admin/keys runtime
// key-management API. Never the same secret as the gateway or provider keys.
const ADMIN_KEY_STORAGE = "rekai.adminKey";

export function getStoredAdminKey(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(ADMIN_KEY_STORAGE) || "";
}

export function setStoredAdminKey(value: string): void {
  if (typeof window === "undefined") return;
  if (value) {
    window.localStorage.setItem(ADMIN_KEY_STORAGE, value);
  } else {
    window.localStorage.removeItem(ADMIN_KEY_STORAGE);
  }
}

export interface AdminKeyList {
  static: string[];
  dynamic: string[];
}

export interface AdminKeyActionResponse {
  status: "added" | "revoked";
  key: string;
}

export async function fetchAdminKeys(adminKey: string): Promise<AdminKeyList> {
  const res = await fetch(`${API_URL}/admin/keys`, {
    cache: "no-store",
    headers: gatewayAuthHeaders(adminKey),
  });
  if (!res.ok) throw await errorFromResponse(res);
  return res.json();
}

export async function addAdminKey(
  adminKey: string,
  key: string,
): Promise<AdminKeyActionResponse> {
  const res = await fetch(`${API_URL}/admin/keys`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...gatewayAuthHeaders(adminKey) },
    body: JSON.stringify({ key }),
  });
  if (!res.ok) throw await errorFromResponse(res);
  return res.json();
}

export async function revokeAdminKey(
  adminKey: string,
  key: string,
): Promise<AdminKeyActionResponse> {
  const res = await fetch(`${API_URL}/admin/keys/${encodeURIComponent(key)}`, {
    method: "DELETE",
    headers: gatewayAuthHeaders(adminKey),
  });
  if (!res.ok) throw await errorFromResponse(res);
  return res.json();
}

export async function sendChat(params: {
  model: string;
  messages: ChatMessage[];
  providerKey?: string;
  gatewayKey?: string;
  temperature?: number;
  maxTokens?: number;
  provider?: string;
  onRateLimit?: (info: RateLimitInfo) => void;
}): Promise<ChatResponse> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...gatewayAuthHeaders(params.gatewayKey),
  };
  if (params.providerKey) headers["X-Provider-Key"] = params.providerKey;

  const res = await fetch(`${API_URL}/v1/chat`, {
    method: "POST",
    headers,
    body: JSON.stringify({
      model: params.model,
      messages: params.messages,
      ...(params.provider ? { provider: params.provider } : {}),
      ...(params.temperature != null ? { temperature: params.temperature } : {}),
      ...(params.maxTokens ? { max_tokens: params.maxTokens } : {}),
    }),
  });

  params.onRateLimit?.(parseRateLimit(res));
  if (!res.ok) {
    throw await errorFromResponse(res);
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
    gatewayKey?: string;
    temperature?: number;
    maxTokens?: number;
    provider?: string;
    onRateLimit?: (info: RateLimitInfo) => void;
  },
  onDelta: (text: string) => void,
  signal?: AbortSignal,
  onSummary?: (summary: StreamSummary) => void,
): Promise<void> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...gatewayAuthHeaders(params.gatewayKey),
  };
  if (params.providerKey) headers["X-Provider-Key"] = params.providerKey;

  const res = await fetch(`${API_URL}/v1/chat/stream`, {
    method: "POST",
    headers,
    body: JSON.stringify({
      model: params.model,
      messages: params.messages,
      ...(params.provider ? { provider: params.provider } : {}),
      ...(params.temperature != null ? { temperature: params.temperature } : {}),
      ...(params.maxTokens ? { max_tokens: params.maxTokens } : {}),
    }),
    signal,
  });

  params.onRateLimit?.(parseRateLimit(res));
  if (!res.ok || !res.body) {
    throw await errorFromResponse(res);
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
      else if (ev.kind === "summary") onSummary?.(ev.summary);
      else if (ev.kind === "error") throw new Error(ev.message);
    }
  }
}

export interface StreamSummary {
  provider: string;
  model: string;
  usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number };
  cost_usd: number | null;
  estimated: boolean;
  tool_calls?: Record<string, unknown>[];
}

export type SSEEvent =
  | { kind: "delta"; text: string }
  | { kind: "summary"; summary: StreamSummary }
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
    if (event.usage) return { kind: "summary", summary: event as StreamSummary };
    return { kind: "ignore" };
  } catch {
    return { kind: "ignore" };
  }
}

export interface EmbeddingsResponse {
  provider: string;
  model: string;
  embeddings: number[][];
  usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number };
  cost_usd: number | null;
  cached: boolean;
}

export async function sendEmbeddings(params: {
  model: string;
  input: string[];
  providerKey?: string;
  gatewayKey?: string;
  provider?: string;
}): Promise<EmbeddingsResponse> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...gatewayAuthHeaders(params.gatewayKey),
  };
  if (params.providerKey) headers["X-Provider-Key"] = params.providerKey;

  const res = await fetch(`${API_URL}/v1/embeddings`, {
    method: "POST",
    headers,
    body: JSON.stringify({
      model: params.model,
      input: params.input,
      ...(params.provider ? { provider: params.provider } : {}),
    }),
  });

  if (!res.ok) {
    throw await errorFromResponse(res);
  }
  return res.json();
}

/** Cosine similarity of two equal-length vectors. Pure, for unit testing. */
export function cosineSimilarity(a: number[], b: number[]): number {
  if (a.length !== b.length || a.length === 0) return 0;
  let dot = 0;
  let na = 0;
  let nb = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  if (na === 0 || nb === 0) return 0;
  return dot / (Math.sqrt(na) * Math.sqrt(nb));
}

export async function fetchModels(
  type?: "chat" | "embedding",
  gatewayKey?: string,
): Promise<ModelInfo[]> {
  try {
    const url = type ? `${API_URL}/v1/models?type=${type}` : `${API_URL}/v1/models`;
    const res = await fetch(url, { headers: gatewayAuthHeaders(gatewayKey) });
    if (!res.ok) return [];
    const body = await res.json();
    return body.data ?? [];
  } catch {
    return [];
  }
}

export async function fetchUsage(gatewayKey?: string): Promise<UsageSummary> {
  const res = await fetch(`${API_URL}/v1/usage`, {
    cache: "no-store",
    headers: gatewayAuthHeaders(gatewayKey),
  });
  if (!res.ok) throw await errorFromResponse(res);
  return res.json();
}

export async function fetchHealth(): Promise<HealthResponse | null> {
  try {
    const res = await fetch(`${API_URL}/health`, { cache: "no-store" });
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}
