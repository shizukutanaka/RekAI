export interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

export interface FallbackTarget {
  provider: string;
  model?: string;
}

export interface StreamSummary {
  provider: string;
  model: string;
  usage: Usage;
  cost_usd: number | null;
  estimated: boolean;
  tool_calls?: Record<string, unknown>[];
}

export interface ChatOptions {
  provider?: string;
  temperature?: number;
  maxTokens?: number;
  cache?: boolean;
  fallbacks?: FallbackTarget[];
  providerKey?: string;
  gatewayKey?: string;
  /** OpenAI-style tool/function definitions, passed through. */
  tools?: Record<string, unknown>[];
  /** Tool choice ('auto' | 'none' | 'required' | object), passed through. */
  toolChoice?: unknown;
  /**
   * OpenAI-style response_format ({ type: 'json_object' } or
   * { type: 'json_schema', json_schema: {...} }), passed through to providers
   * that support it (OpenAI/OpenAI-compatible natively, Gemini best-effort).
   */
  responseFormat?: Record<string, unknown>;
  /** Called once with the final usage summary during streaming. */
  onUsage?: (summary: StreamSummary) => void;
}

export interface Usage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface ChatResult {
  id: string;
  provider: string;
  model: string;
  content: string;
  tool_calls: Record<string, unknown>[] | null;
  usage: Usage;
  cost_usd: number | null;
  cached: boolean;
  /**
   * Why the model stopped, normalized across providers. `"length"` means the
   * answer was cut off by `max_tokens` and is INCOMPLETE — retry with a larger
   * budget. `null` when the provider didn't report one.
   */
  finish_reason: "stop" | "length" | "tool_calls" | "content_filter" | null;
  /**
   * Cosine similarity to the stored prompt when the semantic cache served this
   * response — i.e. the answer is to a *similar* prompt, not this one. Null on
   * a miss and on an exact cache hit, so a non-null value is exactly the signal
   * that an approximate match was used.
   */
  cache_similarity: number | null;
  fallback_used: boolean;
  /** Secret patterns scrubbed from `content` by the output-redaction guardrail. */
  redacted: string[] | null;
  /** Unix timestamp the gateway produced the response. */
  created: number;
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

export interface EmbeddingsOptions {
  provider?: string;
  cache?: boolean;
  providerKey?: string;
  gatewayKey?: string;
}

export interface EmbeddingsResult {
  provider: string;
  model: string;
  embeddings: number[][];
  usage: Usage;
  cost_usd: number | null;
  cached: boolean;
}

export interface UsageSummary {
  requests_total: number;
  cache_hits_total: number;
  cache_misses_total: number;
  /** Subset of cache_hits_total served by approximate (embedding) match. */
  semantic_cache_hits_total: number;
  errors_total: number;
  fallbacks_total: number;
  tokens_total: number;
  cost_usd_total: number;
  requests_by_provider: Record<string, number>;
  /** Transient upstream failures retried in place. */
  retries_total: number;
  /** Providers parked after a 429 or repeated 5xx. */
  cooldowns_total: number;
  /** Per-client volume and spend, keyed by the non-reversible client id. */
  usage_by_client: Record<
    string,
    { requests: number; tokens: number; cost_usd: number }
  >;
}

export class RekAIError extends Error {
  statusCode?: number;
  constructor(message: string, statusCode?: number);
}

export type Messages = string | ChatMessage[];

export interface RekAIClientOptions {
  providerKey?: string;
  gatewayKey?: string;
}

export class RekAIClient {
  baseUrl: string;
  providerKey?: string;
  gatewayKey?: string;
  constructor(baseUrl?: string, options?: RekAIClientOptions);
  chat(model: string, messages: Messages, opts?: ChatOptions): Promise<ChatResult>;
  stream(model: string, messages: Messages, opts?: ChatOptions): AsyncGenerator<string>;
  embeddings(
    model: string,
    input: string | string[],
    opts?: EmbeddingsOptions,
  ): Promise<EmbeddingsResult>;
  models(opts?: { gatewayKey?: string }): Promise<ModelInfo[]>;
  usage(opts?: { gatewayKey?: string }): Promise<UsageSummary>;
  health(): Promise<Record<string, unknown>>;
}

export default RekAIClient;
