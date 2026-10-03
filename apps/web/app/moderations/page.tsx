"use client";

import { useMemo, useState } from "react";
import {
  ModerationsResponse,
  getStoredGatewayKey,
  getStoredKey,
  sendModerations,
} from "@/lib/api";

const SAMPLE = "I had a great time at the park today.";

export default function ModerationsPage() {
  const [model, setModel] = useState("omni-moderation-latest");
  const [provider, setProvider] = useState("");
  const [text, setText] = useState(SAMPLE);
  const [result, setResult] = useState<ModerationsResponse | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const inputs = useMemo(
    () =>
      text
        .split("\n")
        .map((l) => l.trim())
        .filter(Boolean),
    [text],
  );

  async function run() {
    setLoading(true);
    setError("");
    try {
      const data = await sendModerations({
        input: inputs,
        model: model.trim() || undefined,
        provider: provider.trim() || undefined,
        providerKey: getStoredKey() || undefined,
        gatewayKey: getStoredGatewayKey() || undefined,
      });
      setResult(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Moderation failed");
      setResult(null);
    } finally {
      setLoading(false);
    }
  }

  const flaggedCount = result?.results.filter((r) => r.flagged).length ?? 0;

  return (
    <div className="page">
      <div className="page-head">
        <h2>Moderations</h2>
        <div className="page-head-meta">
          <button onClick={run} disabled={loading || inputs.length === 0}>
            {loading ? "Checking…" : "Check"}
          </button>
        </div>
      </div>

      <p className="hint">
        Screen text against a moderation model — OpenAI-compatible
        <code> POST /v1/moderations</code>. Provider <code>echo</code> runs the
        deterministic offline stub; the default model routes to openai and needs
        a key in Settings. One input per line.
      </p>

      <div className="field">
        <label htmlFor="model">Model</label>
        <input
          id="model"
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder="omni-moderation-latest"
        />
      </div>

      <div className="field">
        <label htmlFor="provider">Provider (optional)</label>
        <input
          id="provider"
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
          placeholder="route by model name (try echo offline)"
        />
      </div>

      <div className="field">
        <label htmlFor="text">Inputs ({inputs.length})</label>
        <textarea
          id="text"
          rows={6}
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
      </div>

      {error && <div className="error">{error}</div>}

      {result && (
        <>
          <div className="cards">
            <div className="card">
              <div className="card-value">{result.results.length}</div>
              <div className="card-label">Inputs checked</div>
            </div>
            <div className="card">
              <div className="card-value">{flaggedCount}</div>
              <div className="card-label">Flagged</div>
            </div>
            <div className="card">
              <div className="card-value">{result.provider}</div>
              <div className="card-label">Provider</div>
              <div className="card-sub">{result.model}</div>
            </div>
          </div>

          <h3 className="section">Results</h3>
          <ul className="providers">
            {result.results.map((r, i) => {
              const hits = Object.entries(r.categories ?? {})
                .filter(([, v]) => v)
                .map(([k]) => k);
              const scores = Object.entries(r.category_scores ?? {})
                .sort((a, b) => b[1] - a[1])
                .slice(0, 3);
              return (
                <li key={i}>
                  <div>
                    <strong>#{i + 1}</strong>{" "}
                    <span className={`badge ${r.flagged ? "flagged" : "ready"}`}>
                      {r.flagged ? "flagged" : "ok"}
                    </span>{" "}
                    <span className="hint">{inputs[i] ?? ""}</span>
                  </div>
                  {hits.length > 0 && (
                    <div className="hint">categories: {hits.join(", ")}</div>
                  )}
                  {scores.length > 0 && (
                    <div className="hint">
                      top scores:{" "}
                      {scores.map(([k, v]) => `${k} ${v.toFixed(3)}`).join(" · ")}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      )}
    </div>
  );
}
