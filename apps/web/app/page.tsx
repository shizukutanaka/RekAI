"use client";

import { useEffect, useRef, useState } from "react";
import {
  ChatMessage,
  HealthResponse,
  ModelInfo,
  RateLimitInfo,
  StreamSummary,
  fetchHealth,
  fetchModels,
  formatCost,
  getStoredGatewayKey,
  getStoredKey,
  modelsOfType,
  sendChat,
  streamChat,
} from "@/lib/api";

interface DisplayMessage extends ChatMessage {
  provider?: string;
  cached?: boolean;
  tokens?: number;
  cost?: number | null;
  streaming?: boolean;
}

export default function ChatPage() {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [model, setModel] = useState("echo");
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [loading, setLoading] = useState(false);
  const [streaming, setStreaming] = useState(true);
  const [system, setSystem] = useState("");
  const [temperature, setTemperature] = useState(0.7);
  const [maxTokens, setMaxTokens] = useState("");
  const [showOptions, setShowOptions] = useState(false);
  const [error, setError] = useState("");
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [hasKey, setHasKey] = useState(false);
  const [rateLimit, setRateLimit] = useState<RateLimitInfo | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  const HISTORY_KEY = "rekai.conversation";

  useEffect(() => {
    fetchModels(undefined, getStoredGatewayKey() || undefined).then((m) => {
      if (m.length) setModels(m);
    });
    fetchHealth().then(setHealth);
    setHasKey(Boolean(getStoredKey()));
    // Re-check the stored key when returning to the tab (it may be set elsewhere).
    const onFocus = () => setHasKey(Boolean(getStoredKey()));
    window.addEventListener("focus", onFocus);
    // Restore a previous conversation, if any.
    try {
      const saved = window.localStorage.getItem(HISTORY_KEY);
      if (saved) setMessages(JSON.parse(saved));
    } catch {
      /* ignore malformed history */
    }
    return () => window.removeEventListener("focus", onFocus);
  }, []);

  // The provider that the selected model routes to (from /v1/models), if known.
  const selectedProvider = models.find((m) => m.id === model)?.provider;
  // Chat selector excludes embedding-only models (they live on /embeddings).
  const chatModels = modelsOfType(models, "chat");
  const needsKey =
    selectedProvider != null &&
    health?.provider_status[selectedProvider] === "byok_only" &&
    !hasKey;

  // Persist the conversation (skip while a stream is mid-flight).
  useEffect(() => {
    if (loading) return;
    try {
      window.localStorage.setItem(HISTORY_KEY, JSON.stringify(messages));
    } catch {
      /* ignore quota errors */
    }
  }, [messages, loading]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  function stop() {
    abortRef.current?.abort();
  }

  function clearConversation() {
    setMessages([]);
    try {
      window.localStorage.removeItem(HISTORY_KEY);
    } catch {
      /* ignore */
    }
  }

  // Run a completion for a conversation ending in a user message, appending the
  // assistant reply. Shared by send() and regenerate().
  async function runChat(history: DisplayMessage[]) {
    setError("");
    setLoading(true);

    const convo = history.map(({ role, content }) => ({ role, content }));
    // Prepend an optional system prompt (not shown as a chat bubble).
    const wire = system.trim()
      ? [{ role: "system" as const, content: system.trim() }, ...convo]
      : convo;
    const providerKey = getStoredKey();
    const gatewayKey = getStoredGatewayKey() || undefined;
    const mt = parseInt(maxTokens, 10);
    const maxTokensNum = Number.isFinite(mt) && mt > 0 ? mt : undefined;

    try {
      if (streaming) {
        // Append a placeholder assistant bubble and fill it as deltas arrive.
        setMessages([...history, { role: "assistant", content: "", streaming: true }]);
        const controller = new AbortController();
        abortRef.current = controller;
        let summary: StreamSummary | null = null;
        try {
          await streamChat(
            {
              model,
              messages: wire,
              providerKey,
              gatewayKey,
              temperature,
              maxTokens: maxTokensNum,
              provider: selectedProvider,
              onRateLimit: setRateLimit,
            },
            (delta) => {
              setMessages((prev) => {
                const next = [...prev];
                const last = next[next.length - 1];
                if (last?.role === "assistant") {
                  next[next.length - 1] = { ...last, content: last.content + delta };
                }
                return next;
              });
            },
            controller.signal,
            (s) => {
              summary = s;
            },
          );
        } catch (e) {
          // A user-initiated stop is not an error — keep what streamed so far.
          if (!(e instanceof DOMException && e.name === "AbortError")) throw e;
        }
        // Finalize the bubble (mark complete; note if it was stopped early).
        const wasAborted = controller.signal.aborted;
        const finalSummary = summary as StreamSummary | null;
        setMessages((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last?.role === "assistant") {
            next[next.length - 1] = {
              ...last,
              streaming: false,
              provider: wasAborted ? `${model} · stopped` : model,
              tokens: finalSummary?.usage.total_tokens,
              cost: finalSummary?.cost_usd ?? undefined,
            };
          }
          return next;
        });
      } else {
        const res = await sendChat({
          model,
          messages: wire,
          providerKey,
          gatewayKey,
          temperature,
          maxTokens: maxTokensNum,
          provider: selectedProvider,
          onRateLimit: setRateLimit,
        });
        setMessages([
          ...history,
          {
            role: "assistant",
            content: res.content,
            provider: res.provider,
            cached: res.cached,
            tokens: res.usage.total_tokens,
            cost: res.cost_usd,
          },
        ]);
      }
    } catch (e) {
      // Drop the half-filled streaming bubble, if any, and surface the error.
      setMessages((prev) =>
        prev.filter((m) => !(m.role === "assistant" && m.streaming)),
      );
      setError(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      abortRef.current = null;
      setLoading(false);
    }
  }

  async function send() {
    const content = input.trim();
    if (!content || loading) return;
    const history: DisplayMessage[] = [...messages, { role: "user", content }];
    setMessages(history);
    setInput("");
    await runChat(history);
  }

  function regenerate() {
    if (loading) return;
    // Drop the last assistant reply and re-run from the last user message.
    const lastUserIdx = messages.map((m) => m.role).lastIndexOf("user");
    if (lastUserIdx === -1) return;
    const history = messages.slice(0, lastUserIdx + 1);
    setMessages(history);
    runChat(history);
  }

  const canRegenerate =
    !loading &&
    messages.length > 0 &&
    messages[messages.length - 1].role === "assistant";

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  return (
    <>
      <div className="controls">
        <label htmlFor="model">Model</label>
        {chatModels.length ? (
          <select id="model" value={model} onChange={(e) => setModel(e.target.value)}>
            {chatModels.map((m) => (
              <option key={`${m.provider}:${m.id}`} value={m.id}>
                {m.id} ({m.provider})
              </option>
            ))}
          </select>
        ) : (
          <input id="model" value={model} onChange={(e) => setModel(e.target.value)} />
        )}
        <label className="toggle">
          <input
            type="checkbox"
            checked={streaming}
            onChange={(e) => setStreaming(e.target.checked)}
          />
          Stream
        </label>
        <button
          className="ghost"
          onClick={() => setShowOptions((v) => !v)}
          aria-expanded={showOptions}
        >
          Options {showOptions ? "▲" : "▼"}
        </button>
        {messages.length > 0 && (
          <button onClick={clearConversation} style={{ marginLeft: "auto" }}>
            Clear
          </button>
        )}
      </div>

      {showOptions && (
        <div className="options">
          <div className="field">
            <label htmlFor="system">System prompt</label>
            <textarea
              id="system"
              rows={2}
              value={system}
              placeholder="Optional. e.g. You are a terse assistant."
              onChange={(e) => setSystem(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="temp">
              Temperature: <strong>{temperature.toFixed(1)}</strong>
            </label>
            <input
              id="temp"
              type="range"
              min={0}
              max={2}
              step={0.1}
              value={temperature}
              onChange={(e) => setTemperature(Number(e.target.value))}
            />
          </div>
          <div className="field">
            <label htmlFor="maxtok">Max tokens</label>
            <input
              id="maxtok"
              type="number"
              min={1}
              value={maxTokens}
              placeholder="default (provider limit)"
              onChange={(e) => setMaxTokens(e.target.value)}
            />
          </div>
        </div>
      )}

      <div className="messages">
        {messages.length === 0 && (
          <div className="empty">
            Start chatting. The default <code>echo</code> model needs no API key.
            <br />
            Add your own key under <strong>Settings</strong> to use OpenAI.
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            {m.content}
            {m.streaming && <span className="cursor">▌</span>}
            {m.role === "assistant" && !m.streaming && (
              <span className="meta">
                {m.provider}
                {m.cached ? " · cached ⚡" : ""}
                {typeof m.tokens === "number" ? ` · ${m.tokens} tokens` : ""}
                {formatCost(m.cost) ? ` · ${formatCost(m.cost)}` : ""}
              </span>
            )}
          </div>
        ))}
        {loading && !streaming && <div className="msg assistant">…</div>}
        {canRegenerate && (
          <button className="ghost regenerate" onClick={regenerate}>
            ↻ Regenerate
          </button>
        )}
        <div ref={bottomRef} />
      </div>

      {needsKey && (
        <div className="notice">
          <strong>{selectedProvider}</strong> needs an API key. Add one under{" "}
          <a href="/settings">Settings</a> (BYOK) to use this model.
        </div>
      )}

      {error && <div className="error">{error}</div>}

      <div className="composer">
        <textarea
          value={input}
          placeholder="Type a message… (Enter to send, Shift+Enter for newline)"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
        />
        {loading && streaming ? (
          <button onClick={stop} className="stop">
            Stop
          </button>
        ) : (
          <button onClick={send} disabled={loading || !input.trim()}>
            Send
          </button>
        )}
      </div>

      {rateLimit?.remaining != null && rateLimit.limit != null && (
        <p className="hint rate-hint">
          {rateLimit.remaining} / {rateLimit.limit} requests left in the rate-limit window
        </p>
      )}
    </>
  );
}
