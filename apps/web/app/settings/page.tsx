"use client";

import { useEffect, useState } from "react";
import {
  API_URL,
  HealthResponse,
  fetchHealth,
  getStoredGatewayKey,
  getStoredKey,
  setStoredGatewayKey,
  setStoredKey,
} from "@/lib/api";

export default function SettingsPage() {
  const [key, setKey] = useState("");
  const [gatewayKey, setGatewayKey] = useState("");
  const [saved, setSaved] = useState(false);
  const [health, setHealth] = useState<HealthResponse | null>(null);

  useEffect(() => {
    setKey(getStoredKey());
    setGatewayKey(getStoredGatewayKey());
    fetchHealth().then(setHealth);
  }, []);

  function save() {
    setStoredKey(key.trim());
    setStoredGatewayKey(gatewayKey.trim());
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  }

  return (
    <div className="page">
      <h2>Settings</h2>

      <div className="field">
        <label htmlFor="key">Provider API key (BYOK)</label>
        <input
          id="key"
          type="password"
          value={key}
          placeholder="sk-..."
          onChange={(e) => setKey(e.target.value)}
        />
        <p className="hint">
          Stored only in your browser&apos;s local storage and sent to the API as the{" "}
          <code>X-Provider-Key</code> header. RekAI never persists it server-side.
        </p>
      </div>

      <div className="field">
        <label htmlFor="gatewayKey">Gateway API key</label>
        <input
          id="gatewayKey"
          type="password"
          value={gatewayKey}
          placeholder="sk-rekai-..."
          onChange={(e) => setGatewayKey(e.target.value)}
        />
        <p className="hint">
          Only needed if this RekAI deployment has <code>REKAI_API_KEYS</code>{" "}
          configured. Sent as an <code>Authorization: Bearer</code> header. Different
          from the provider key above — this authenticates you to the gateway
          itself, not to an upstream provider like OpenAI.
        </p>
      </div>

      <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
        <button onClick={save}>Save</button>
        {saved && <span className="saved">Saved ✓</span>}
      </div>

      {health && (
        <div className="field" style={{ marginTop: 32 }}>
          <label>Providers</label>
          <ul className="providers">
            {Object.entries(health.provider_status).map(([name, status]) => (
              <li key={name}>
                <span>{name}</span>
                <span className={`badge ${status}`}>
                  {status === "ready" ? "ready" : "needs key"}
                </span>
              </li>
            ))}
          </ul>
          <p className="hint">
            <strong>ready</strong> works without a key; <strong>needs key</strong>{" "}
            requires a BYOK key above (or a server-side key).
          </p>
        </div>
      )}

      <div className="field" style={{ marginTop: 32 }}>
        <label>API endpoint</label>
        <p className="hint">
          Connected to <code>{API_URL}</code>
          {health ? ` · v${health.version} · cache: ${health.cache}` : ""}. Change
          it with <code>NEXT_PUBLIC_API_URL</code>.
        </p>
      </div>
    </div>
  );
}
