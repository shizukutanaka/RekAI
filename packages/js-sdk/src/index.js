// Official JavaScript/TypeScript client for the RekAI gateway.
// Zero dependencies — uses the global `fetch` (Node 18+ or any modern browser).

export class RekAIError extends Error {
  /** @param {string} message @param {number} [statusCode] */
  constructor(message, statusCode) {
    super(message);
    this.name = "RekAIError";
    this.statusCode = statusCode;
  }
}

/** @param {string|Array<{role:string,content:string}>} messages */
function normalize(messages) {
  if (typeof messages === "string") {
    return [{ role: "user", content: messages }];
  }
  return messages;
}

// HTTP statuses worth retrying: rate limiting and transient upstream failures.
// Any other 4xx is the client's fault and is never retried.
const RETRYABLE_STATUS = new Set([429, 502, 503, 504]);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** A random id, so an auto-retried request can't double-execute server-side. */
function randomIdempotencyKey() {
  const uuid = globalThis.crypto?.randomUUID?.();
  return `rekai-sdk-${uuid ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
}

export class RekAIClient {
  /**
   * @param {string} [baseUrl]
   * @param {{ providerKey?: string, gatewayKey?: string, maxRetries?: number,
   *           retryBackoff?: number }} [options]
   */
  constructor(baseUrl = "http://localhost:8000", options = {}) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.providerKey = options.providerKey;
    this.gatewayKey = options.gatewayKey;
    // Client-side retry for transient failures (network errors, 429/5xx).
    this.maxRetries = options.maxRetries ?? 2;
    this.retryBackoff = options.retryBackoff ?? 0.5; // seconds, doubled per attempt
  }

  /**
   * @param {string} [providerKey]
   * @param {string} [gatewayKey]
   * @param {string} [idempotencyKey]
   */
  _headers(providerKey, gatewayKey, idempotencyKey) {
    const headers = { "Content-Type": "application/json" };
    const key = providerKey || this.providerKey;
    if (key) headers["X-Provider-Key"] = key;
    // The gateway key authenticates this client to RekAI (REKAI_API_KEYS);
    // distinct from the provider key above, which is BYOK for the upstream
    // provider. Required on /v1/* whenever the deployment has keys configured.
    const bearer = gatewayKey || this.gatewayKey;
    if (bearer) headers["Authorization"] = `Bearer ${bearer}`;
    // Idempotency-Key lets the server replay the first response on a retry
    // instead of re-processing (so a retried request can't double-charge).
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    return headers;
  }

  /** Milliseconds to wait before the next attempt: Retry-After, else backoff. */
  _retryDelayMs(res, attempt) {
    if (res) {
      const raw = res.headers.get("Retry-After");
      const secs = raw != null ? Number(raw) : NaN;
      if (Number.isFinite(secs)) return Math.max(0, secs) * 1000;
    }
    return this.retryBackoff * 2 ** attempt * 1000;
  }

  /**
   * Issue a request, retrying transient failures with exponential backoff.
   * Retries on network errors and on 429/502/503/504, honoring `Retry-After`.
   * @param {string} path @param {RequestInit} init @returns {Promise<Response>}
   */
  async _send(path, init) {
    let attempt = 0;
    for (;;) {
      let res;
      try {
        res = await fetch(`${this.baseUrl}${path}`, init);
      } catch (err) {
        if (attempt >= this.maxRetries) throw err;
        await sleep(this._retryDelayMs(null, attempt));
        attempt++;
        continue;
      }
      if (RETRYABLE_STATUS.has(res.status) && attempt < this.maxRetries) {
        await sleep(this._retryDelayMs(res, attempt));
        attempt++;
        continue;
      }
      return res;
    }
  }

  _payload(model, messages, opts) {
    const payload = {
      model,
      messages: normalize(messages),
      temperature: opts.temperature ?? 0.7,
      cache: opts.cache ?? true,
    };
    if (opts.provider != null) payload.provider = opts.provider;
    if (opts.maxTokens != null) payload.max_tokens = opts.maxTokens;
    if (opts.fallbacks != null) payload.fallbacks = opts.fallbacks;
    if (opts.tools != null) payload.tools = opts.tools;
    if (opts.toolChoice != null) payload.tool_choice = opts.toolChoice;
    if (opts.responseFormat != null) payload.response_format = opts.responseFormat;
    return payload;
  }

  async _raiseForStatus(res) {
    if (res.ok) return;
    let detail = `RekAI returned ${res.status}`;
    try {
      const body = await res.json();
      detail = body.detail || body.error || detail;
    } catch {
      /* ignore */
    }
    throw new RekAIError(detail, res.status);
  }

  /**
   * Send a chat completion.
   *
   * `opts.idempotencyKey` is sent as the `Idempotency-Key` header so the server
   * replays the first response on a retry instead of re-processing. When retries
   * are enabled (`maxRetries > 0`) and none is given, one is generated so an
   * auto-retried request can't double-run.
   * @param {string} model
   * @param {string|Array<{role:string,content:string}>} messages
   * @param {object} [opts]
   * @returns {Promise<object>}
   */
  async chat(model, messages, opts = {}) {
    const idempotencyKey =
      opts.idempotencyKey ?? (this.maxRetries > 0 ? randomIdempotencyKey() : undefined);
    const res = await this._send("/v1/chat", {
      method: "POST",
      headers: this._headers(opts.providerKey, opts.gatewayKey, idempotencyKey),
      body: JSON.stringify(this._payload(model, messages, opts)),
    });
    await this._raiseForStatus(res);
    return res.json();
  }

  /**
   * Stream a chat completion, yielding text chunks. If `opts.onUsage` is given,
   * it is called once with the final usage summary when the server reports it.
   * @param {string} model
   * @param {string|Array<{role:string,content:string}>} messages
   * @param {object} [opts]
   * @returns {AsyncGenerator<string>}
   */
  async *stream(model, messages, opts = {}) {
    const res = await fetch(`${this.baseUrl}/v1/chat/stream`, {
      method: "POST",
      headers: this._headers(opts.providerKey, opts.gatewayKey),
      body: JSON.stringify(this._payload(model, messages, opts)),
    });
    await this._raiseForStatus(res);
    if (!res.body) return;

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let sep;
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
        if (!dataLine) continue;
        const payload = dataLine.slice("data:".length).trim();
        if (payload === "[DONE]") return;
        let event;
        try {
          event = JSON.parse(payload);
        } catch {
          continue;
        }
        if (event.delta) yield event.delta;
        else if (event.usage) opts.onUsage?.(event);
        else if (event.error) throw new RekAIError(event.detail || event.error);
      }
    }
  }

  /**
   * Create embeddings for a string or array of strings.
   * @param {string} model
   * @param {string|string[]} input
   * @param {object} [opts]
   * @returns {Promise<object>}
   */
  async embeddings(model, input, opts = {}) {
    const payload = { model, input, cache: opts.cache ?? true };
    if (opts.provider != null) payload.provider = opts.provider;
    const res = await this._send("/v1/embeddings", {
      method: "POST",
      headers: this._headers(opts.providerKey, opts.gatewayKey),
      body: JSON.stringify(payload),
    });
    await this._raiseForStatus(res);
    return res.json();
  }

  /**
   * @param {{ gatewayKey?: string }} [opts]
   * @returns {Promise<Array<{id:string,provider:string}>>}
   */
  async models(opts = {}) {
    const res = await this._send("/v1/models", {
      headers: this._headers(undefined, opts.gatewayKey),
    });
    await this._raiseForStatus(res);
    return (await res.json()).data ?? [];
  }

  /**
   * @param {{ gatewayKey?: string }} [opts]
   * @returns {Promise<object>}
   */
  async usage(opts = {}) {
    const res = await this._send("/v1/usage", {
      headers: this._headers(undefined, opts.gatewayKey),
    });
    await this._raiseForStatus(res);
    return res.json();
  }

  /** @returns {Promise<object>} */
  async health() {
    const res = await this._send("/health", {});
    await this._raiseForStatus(res);
    return res.json();
  }
}

export default RekAIClient;
