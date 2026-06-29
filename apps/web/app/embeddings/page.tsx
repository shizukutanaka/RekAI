"use client";

import { useEffect, useMemo, useState } from "react";
import {
  EmbeddingsResponse,
  ModelInfo,
  cosineSimilarity,
  fetchModels,
  formatCost,
  getStoredKey,
  sendEmbeddings,
} from "@/lib/api";

const SAMPLE = `a cat sat on the mat
a feline rested on the rug
quarterly revenue grew`;

export default function EmbeddingsPage() {
  const [model, setModel] = useState("echo");
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [text, setText] = useState(SAMPLE);
  const [result, setResult] = useState<EmbeddingsResponse | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    fetchModels("embedding").then((m) => {
      if (m.length) setModels(m);
    });
  }, []);

  const inputs = useMemo(
    () => text.split("\n").map((l) => l.trim()).filter(Boolean),
    [text],
  );

  // The provider the selected model routes to (from /v1/models), if known.
  const selectedProvider = models.find((m) => m.id === model)?.provider;

  async function run() {
    setLoading(true);
    setError("");
    try {
      const data = await sendEmbeddings({
        model: model.trim() || "echo",
        input: inputs,
        provider: selectedProvider,
        providerKey: getStoredKey() || undefined,
      });
      setResult(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to embed");
      setResult(null);
    } finally {
      setLoading(false);
    }
  }

  // Pairwise cosine similarities between the returned vectors.
  const pairs = useMemo(() => {
    if (!result) return [];
    const out: { i: number; j: number; sim: number }[] = [];
    const v = result.embeddings;
    for (let i = 0; i < v.length; i++) {
      for (let j = i + 1; j < v.length; j++) {
        out.push({ i, j, sim: cosineSimilarity(v[i], v[j]) });
      }
    }
    return out;
  }, [result]);

  return (
    <div className="page">
      <div className="page-head">
        <h2>Embeddings</h2>
        <div className="page-head-meta">
          <button onClick={run} disabled={loading || inputs.length === 0}>
            {loading ? "Embedding…" : "Embed"}
          </button>
        </div>
      </div>

      <p className="hint">
        Turn text into vectors. <code>echo</code> works with no key (deterministic
        vectors); pick an embeddings model (e.g. <code>text-embedding-3-small</code>)
        and set a key in Settings for real ones. One input per line.
      </p>

      <div className="field">
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
          <input
            id="model"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="echo"
          />
        )}
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
              <div className="card-value">{result.embeddings.length}</div>
              <div className="card-label">Vectors</div>
            </div>
            <div className="card">
              <div className="card-value">{result.embeddings[0]?.length ?? 0}</div>
              <div className="card-label">Dimensions</div>
            </div>
            <div className="card">
              <div className="card-value">{result.provider}</div>
              <div className="card-label">Provider</div>
              <div className="card-sub">{result.model}</div>
            </div>
            <div className="card">
              <div className="card-value">{formatCost(result.cost_usd) || "—"}</div>
              <div className="card-label">Cost{result.cached ? " (cached)" : ""}</div>
            </div>
          </div>

          {pairs.length > 0 && (
            <>
              <h3 className="section">Pairwise similarity</h3>
              <div className="bars">
                {pairs.map(({ i, j, sim }) => (
                  <div key={`${i}-${j}`} className="bar-row">
                    <span className="bar-label">
                      #{i + 1} ↔ #{j + 1}
                    </span>
                    <div className="bar-track">
                      <div
                        className="bar-fill"
                        style={{ width: `${Math.max(0, Math.min(1, sim)) * 100}%` }}
                      />
                    </div>
                    <span className="bar-count">{sim.toFixed(3)}</span>
                  </div>
                ))}
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
