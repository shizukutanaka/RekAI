"use client";

import { useCallback, useEffect, useState } from "react";
import { UsageSummary, fetchUsage } from "@/lib/api";

function pct(part: number, whole: number): string {
  if (whole <= 0) return "—";
  return `${Math.round((part / whole) * 100)}%`;
}

export default function UsagePage() {
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [error, setError] = useState("");
  const [updatedAt, setUpdatedAt] = useState<string>("");

  const load = useCallback(async () => {
    try {
      const data = await fetchUsage();
      setUsage(data);
      setError("");
      setUpdatedAt(new Date().toLocaleTimeString());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load usage");
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 5000); // live refresh
    return () => clearInterval(id);
  }, [load]);

  const cacheLookups = usage
    ? usage.cache_hits_total + usage.cache_misses_total
    : 0;

  const cards = usage
    ? [
        { label: "Requests", value: usage.requests_total.toLocaleString() },
        {
          label: "Cache hit rate",
          value: pct(usage.cache_hits_total, cacheLookups),
          sub: `${usage.cache_hits_total} / ${cacheLookups}`,
        },
        { label: "Tokens", value: usage.tokens_total.toLocaleString() },
        {
          label: "Est. cost",
          value: `$${usage.cost_usd_total.toFixed(4)}`,
        },
        { label: "Fallbacks", value: usage.fallbacks_total.toLocaleString() },
        { label: "Errors", value: usage.errors_total.toLocaleString() },
      ]
    : [];

  const providers = usage
    ? Object.entries(usage.requests_by_provider).sort((a, b) => b[1] - a[1])
    : [];

  return (
    <div className="page">
      <div className="page-head">
        <h2>Usage</h2>
        <div className="page-head-meta">
          {updatedAt && <span className="hint">updated {updatedAt}</span>}
          <button onClick={load}>Refresh</button>
        </div>
      </div>

      {error && <div className="error">{error}</div>}

      {!usage && !error && <p className="hint">Loading…</p>}

      {usage && (
        <>
          <div className="cards">
            {cards.map((c) => (
              <div key={c.label} className="card">
                <div className="card-value">{c.value}</div>
                <div className="card-label">{c.label}</div>
                {c.sub && <div className="card-sub">{c.sub}</div>}
              </div>
            ))}
          </div>

          <h3 className="section">Requests by provider</h3>
          {providers.length === 0 ? (
            <p className="hint">No requests yet. Send a message from the chat.</p>
          ) : (
            <div className="bars">
              {providers.map(([name, count]) => (
                <div key={name} className="bar-row">
                  <span className="bar-label">{name}</span>
                  <div className="bar-track">
                    <div
                      className="bar-fill"
                      style={{ width: pct(count, usage.requests_total) }}
                    />
                  </div>
                  <span className="bar-count">{count}</span>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
