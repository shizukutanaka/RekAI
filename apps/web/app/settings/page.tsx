"use client";

import { useEffect, useState } from "react";
import { API_URL, getStoredKey, setStoredKey } from "@/lib/api";

export default function SettingsPage() {
  const [key, setKey] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setKey(getStoredKey());
  }, []);

  function save() {
    setStoredKey(key.trim());
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

      <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
        <button onClick={save}>Save</button>
        {saved && <span className="saved">Saved ✓</span>}
      </div>

      <div className="field" style={{ marginTop: 32 }}>
        <label>API endpoint</label>
        <p className="hint">
          Connected to <code>{API_URL}</code>. Change it with{" "}
          <code>NEXT_PUBLIC_API_URL</code>.
        </p>
      </div>
    </div>
  );
}
