"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ModelInfo,
  fetchModels,
  formatPricing,
  getStoredGatewayKey,
} from "@/lib/api";

type TypeFilter = "all" | "chat" | "embedding";

export default function ModelsPage() {
  const [models, setModels] = useState<ModelInfo[] | null>(null);
  const [filter, setFilter] = useState<TypeFilter>("all");

  const load = useCallback(async () => {
    setModels(await fetchModels(undefined, getStoredGatewayKey() || undefined));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const visible = (models ?? []).filter(
    (m) => filter === "all" || (m.type ?? "chat") === filter,
  );

  // Preserve /v1/models ordering, grouped by provider for scannability.
  const byProvider = new Map<string, ModelInfo[]>();
  for (const m of visible) {
    const list = byProvider.get(m.provider) ?? [];
    list.push(m);
    byProvider.set(m.provider, list);
  }

  return (
    <div className="page">
      <div className="page-head">
        <h2>Models</h2>
        <div className="page-head-meta">
          {(["all", "chat", "embedding"] as const).map((t) => (
            <button
              key={t}
              className={filter === t ? "" : "ghost"}
              onClick={() => setFilter(t)}
            >
              {t === "all" ? "All" : t[0].toUpperCase() + t.slice(1)}
            </button>
          ))}
          <button onClick={load}>Refresh</button>
        </div>
      </div>

      <p className="hint">
        Every model <code>GET /v1/models</code> advertises, with its routing
        provider and per-1M-token price. Unpriced models still route — pricing
        only feeds cost estimates.{" "}
        <code>GET /v1/models?type=chat|embedding</code> filters server-side.
      </p>

      {models === null && <p className="hint">Loading…</p>}

      {models !== null && visible.length === 0 && (
        <p className="hint">
          No models returned — is the API reachable, and (with gateway auth on)
          is a key set under Settings?
        </p>
      )}

      {[...byProvider.entries()].map(([provider, list]) => (
        <div key={provider}>
          <h3 className="section">
            {provider} <span className="card-sub">{list.length}</span>
          </h3>
          <ul className="providers">
            {list.map((m) => (
              <li key={m.id}>
                <span>
                  {m.id} <span className="card-sub">{(m.type ?? "chat")}</span>
                </span>
                <span className="card-sub">{formatPricing(m.pricing)}</span>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}
