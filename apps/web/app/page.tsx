"use client";

import { useEffect, useRef, useState } from "react";
import {
  ChatMessage,
  ModelInfo,
  fetchModels,
  getStoredKey,
  sendChat,
} from "@/lib/api";

interface DisplayMessage extends ChatMessage {
  provider?: string;
  cached?: boolean;
  tokens?: number;
}

export default function ChatPage() {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [model, setModel] = useState("echo");
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [loading, setLoading] = useState(false);
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
    try {
      const res = await sendChat({
        model,
        messages: history.map(({ role, content }) => ({ role, content })),
        providerKey: getStoredKey(),
      });
      setMessages([
        ...history,
        {
          role: "assistant",
          content: res.content,
          provider: res.provider,
          cached: res.cached,
          tokens: res.usage.total_tokens,
        },
      ]);
    } catch (e) {
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
            {m.role === "assistant" && (
              <span className="meta">
                {m.provider}
                {m.cached ? " · cached ⚡" : ""}
                {typeof m.tokens === "number" ? ` · ${m.tokens} tokens` : ""}
              </span>
            )}
          </div>
        ))}
        {loading && <div className="msg assistant">…</div>}
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
