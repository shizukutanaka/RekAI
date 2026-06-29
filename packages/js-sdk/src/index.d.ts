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
}

export interface ChatOptions {
  provider?: string;
  temperature?: number;
  maxTokens?: number;
  cache?: boolean;
  fallbacks?: FallbackTarget[];
  providerKey?: string;
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
  usage: Usage;
  cost_usd: number | null;
  cached: boolean;
  fallback_used: boolean;
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

export class RekAIError extends Error {
  statusCode?: number;
  constructor(message: string, statusCode?: number);
}

export type Messages = string | ChatMessage[];

export interface RekAIClientOptions {
  providerKey?: string;
}

export class RekAIClient {
  baseUrl: string;
  providerKey?: string;
  constructor(baseUrl?: string, options?: RekAIClientOptions);
  chat(model: string, messages: Messages, opts?: ChatOptions): Promise<ChatResult>;
  stream(model: string, messages: Messages, opts?: ChatOptions): AsyncGenerator<string>;
  models(): Promise<ModelInfo[]>;
  usage(): Promise<UsageSummary>;
  health(): Promise<Record<string, unknown>>;
}

export default RekAIClient;
