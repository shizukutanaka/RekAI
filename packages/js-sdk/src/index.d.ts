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
  fallback_used: boolean;
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
  errors_total: number;
  fallbacks_total: number;
  tokens_total: number;
  cost_usd_total: number;
  requests_by_provider: Record<string, number>;
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
