export interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
  /** Anthropic server-side tool blocks echoed back verbatim on assistant turns. */
  extra_blocks?: Record<string, unknown>[];
  /** Ordered verbatim content array for an assistant turn — replays the exact upstream sequence. */
  content_blocks?: Record<string, unknown>[];
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
  system_fingerprint?: string | null;
  service_tier?: string | null;
  /** Which stop sequence ended the turn (Anthropic reports it; OpenAI has
   * no equivalent field, so it's absent for other providers). */
  stop_sequence?: string;
  refusal?: string;
  annotations?: Record<string, unknown>[];
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
  /**
   * Sequences that stop generation, as OpenAI's `stop`. A single string is
   * accepted too; the server normalizes it to a one-element list.
   */
  stop?: string | string[];
  /** OpenAI's `parallel_tool_calls` — allow several tool calls per turn. */
  parallelToolCalls?: boolean;
  /** Nucleus sampling, as OpenAI's `top_p`. */
  topP?: number;
  /** Deterministic-sampling seed, as OpenAI's `seed`. */
  seed?: number;
  /** Token-frequency penalty (-2..2), as OpenAI's `frequency_penalty`. */
  frequencyPenalty?: number;
  /** Token-presence penalty (-2..2), as OpenAI's `presence_penalty`. */
  presencePenalty?: number;
  /** Token-id → bias map, as OpenAI's `logit_bias`. */
  logitBias?: Record<string, number>;
  /**
   * OpenAI's processing tier ('auto' | 'default' | 'flex' | 'priority' |
   * 'scale'). Forwarded to OpenAI-compatible providers only.
   */
  serviceTier?: string;
  /** OpenAI's `web_search_options` — hosted web-search config (context size,
   * user location). OpenAI-compatible providers only. */
  webSearchOptions?: Record<string, unknown>;
  /** The providers' end-user id for abuse detection (OpenAI's `user`,
   * Anthropic's `metadata.user_id`). A routing hint — never a cache key. */
  user?: string;
  /** OpenAI's newer hashed abuse-detection identifier (successor to `user`).
   * OpenAI-compatible providers only. */
  safetyIdentifier?: string;
  /** Anthropic extended thinking config, e.g.
   * `{type: "enabled", budget_tokens: 1024}` — forwarded verbatim to Anthropic
   * upstreams. Thinking blocks come back on `ChatResult.thinking_blocks`. */
  thinking?: Record<string, unknown>;
  /** Called once with the final usage summary during streaming. */
  onUsage?: (summary: StreamSummary) => void;
  /** Called with each refusal-text chunk when the model declines. */
  onRefusal?: (text: string) => void;
  /** Called with citations etc. (e.g. web-search url_citation entries). */
  onAnnotations?: (annotations: Record<string, unknown>[]) => void;
}

export interface Usage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  /** Breakdown of completion_tokens spent on reasoning (o-series, Gemini thinking). */
  reasoning_tokens?: number;
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
  /** Which stop sequence ended the turn, when the provider reports one
   * (Anthropic's `stop_sequence` alongside `stop_reason: "stop_sequence"`).
   * Null for providers that don't say — OpenAI's API has no equivalent field. */
  stop_sequence: string | null;
  /**
   * Cosine similarity to the stored prompt when the semantic cache served this
   * response — i.e. the answer is to a *similar* prompt, not this one. Null on
   * a miss and on an exact cache hit, so a non-null value is exactly the signal
   * that an approximate match was used.
   */
  cache_similarity: number | null;
  fallback_used: boolean;
  /** The model's refusal text when it declined; `content` is empty then. */
  refusal: string | null;
  /** Citations etc. attached to the answer (e.g. web-search url_citation). */
  annotations?: Record<string, unknown>[] | null;
  /** Secret patterns scrubbed from `content` by the output-redaction guardrail. */
  redacted: string[] | null;
  /** Anthropic thinking/redacted_thinking blocks produced before the answer. */
  thinking_blocks: Record<string, unknown>[] | null;
  /** Anthropic citations on the answer's text (web-search sources), verbatim. */
  citations: Record<string, unknown>[] | null;
  /** Anthropic server-side tool blocks (server_tool_use, tool-result blocks, mcp_*). */
  extra_blocks: Record<string, unknown>[] | null;
  /** Message-level fields the provider doesn't map (container, context_management, ...). */
  extra_fields: Record<string, unknown> | null;
  /** The upstream content array verbatim, in emitted order. Echo it back on the next turn. */
  content_blocks: Record<string, unknown>[] | null;
  /** Unix timestamp the gateway produced the response. */
  created: number;
  /** OpenAI backend fingerprint — which config served the call (with `seed`, a determinism aid). */
  system_fingerprint?: string | null;
  /** The service tier that actually handled the call when `service_tier` was "auto". */
  service_tier?: string | null;
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
  /** Output dimensionality for models that support it (OpenAI
   * text-embedding-3+, Gemini via outputDimensionality). */
  dimensions?: number;
  /** OpenAI's encoding_format ("float" | "base64"); OpenAI-compatible
   * providers only. */
  encodingFormat?: string;
  /** OpenAI's end-user id for abuse detection — a routing hint,
   * never a cache key. */
  user?: string;
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
  /** Tokens accounted per provider — same key set as requests_by_provider. */
  tokens_by_provider: Record<string, number>;
  /** Transient upstream failures retried in place. */
  retries_total: number;
  /** Providers parked after a 429 or repeated 5xx. */
  cooldowns_total: number;
  /** Per-client volume and spend, keyed by the non-reversible client id. */
  usage_by_client: Record<
    string,
    { requests: number; tokens: number; cost_usd: number }
  >;
  /** Per-end-user volume and spend, nested under the owning client. */
  usage_by_user: Record<
    string,
    Record<string, { requests: number; tokens: number; cost_usd: number }>
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
  /**
   * Automatic retries for transient failures (network errors, 429/502/503/504),
   * honoring Retry-After. Default 2; set 0 to disable.
   */
  maxRetries?: number;
  /** Seconds between attempts, doubled each retry. Default 0.5. */
  retryBackoff?: number;
  /**
   * Cap (seconds) on honoring a Retry-After. A longer value returns the
   * response instead of sleeping. Default 60.
   */
  maxRetryDelay?: number;
  /**
   * Timeout in seconds: bounds each non-streaming request and the idle gap
   * between stream chunks (not total stream length). Default 60; 0 disables.
   */
  timeout?: number;
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
