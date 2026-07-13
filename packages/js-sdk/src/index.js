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

export class RekAIClient {
  /**
   * @param {string} [baseUrl]
   * @param {{ providerKey?: string, gatewayKey?: string }} [options]
   */
  constructor(baseUrl = "http://localhost:8000", options = {}) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.providerKey = options.providerKey;
    this.gatewayKey = options.gatewayKey;
  }

  /**
   * @param {string} [providerKey]
   * @param {string} [gatewayKey]
   */
  _headers(providerKey, gatewayKey) {
    const headers = { "Content-Type": "application/json" };
    const key = providerKey || this.providerKey;
    if (key) headers["X-Provider-Key"] = key;
    // The gateway key authenticates this client to RekAI (REKAI_API_KEYS);
    // distinct from the provider key above, which is BYOK for the upstream
    // provider. Required on /v1/* whenever the deployment has keys configured.
    const bearer = gatewayKey || this.gatewayKey;
    if (bearer) headers["Authorization"] = `Bearer ${bearer}`;
    return headers;
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
   * @param {string} model
   * @param {string|Array<{role:string,content:string}>} messages
   * @param {object} [opts]
   * @returns {Promise<object>}
   */
  async chat(model, messages, opts = {}) {
    const res = await fetch(`${this.baseUrl}/v1/chat`, {
      method: "POST",
      headers: this._headers(opts.providerKey, opts.gatewayKey),
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
    const res = await fetch(`${this.baseUrl}/v1/embeddings`, {
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
    const res = await fetch(`${this.baseUrl}/v1/models`, {
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
    const res = await fetch(`${this.baseUrl}/v1/usage`, {
      headers: this._headers(undefined, opts.gatewayKey),
    });
    await this._raiseForStatus(res);
    return res.json();
  }

  /** @returns {Promise<object>} */
  async health() {
    const res = await fetch(`${this.baseUrl}/health`);
    await this._raiseForStatus(res);
    return res.json();
  }
}

export default RekAIClient;
