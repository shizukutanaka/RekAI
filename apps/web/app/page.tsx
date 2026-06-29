"use client";

import { useEffect, useRef, useState } from "react";
import {
  ChatMessage,
  ModelInfo,
  fetchModels,
  formatCost,
  getStoredKey,
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
  const [error, setError] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetchModels().then((m) => {
      if (m.length) setModels(m);
    });
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  async function send() {
    const content = input.trim();
    if (!content || loading) return;
    setError("");
    const history: DisplayMessage[] = [...messages, { role: "user", content }];
    setMessages(history);
    setInput("");
    setLoading(true);

    const wire = history.map(({ role, content }) => ({ role, content }));
    const providerKey = getStoredKey();

    try {
      if (streaming) {
        // Append a placeholder assistant bubble and fill it as deltas arrive.
        setMessages([...history, { role: "assistant", content: "", streaming: true }]);
        await streamChat({ model, messages: wire, providerKey }, (delta) => {
          setMessages((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.role === "assistant") {
              next[next.length - 1] = { ...last, content: last.content + delta };
            }
            return next;
          });
        });
        setMessages((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last?.role === "assistant") {
            next[next.length - 1] = { ...last, streaming: false, provider: model };
          }
          return next;
        });
      } else {
        const res = await sendChat({ model, messages: wire, providerKey });
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
      setLoading(false);
    }
  }

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
        {models.length ? (
          <select id="model" value={model} onChange={(e) => setModel(e.target.value)}>
            {models.map((m) => (
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
        {messages.length > 0 && (
          <button onClick={() => setMessages([])} style={{ marginLeft: "auto" }}>
            Clear
          </button>
        )}
      </div>

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
        <div ref={bottomRef} />
      </div>

      {error && <div className="error">{error}</div>}

      <div className="composer">
        <textarea
          value={input}
          placeholder="Type a message… (Enter to send, Shift+Enter for newline)"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
        />
        <button onClick={send} disabled={loading || !input.trim()}>
          Send
        </button>
      </div>
    </>
  );
}
