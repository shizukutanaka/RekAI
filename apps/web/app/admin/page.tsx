"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AdminKeyList,
  addAdminKey,
  fetchAdminKeys,
  getStoredAdminKey,
  revokeAdminKey,
  setStoredAdminKey,
} from "@/lib/api";

export default function AdminPage() {
  const [adminKey, setAdminKey] = useState("");
  const [saved, setSaved] = useState(false);
  const [keys, setKeys] = useState<AdminKeyList | null>(null);
  const [error, setError] = useState("");
  const [notConfigured, setNotConfigured] = useState(false);
  const [newKey, setNewKey] = useState("");
  const [revokeKeyValue, setRevokeKeyValue] = useState("");
  const [actionMsg, setActionMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (key: string) => {
    if (!key) return;
    setError("");
    setNotConfigured(false);
    try {
      setKeys(await fetchAdminKeys(key));
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Failed to load keys";
      // The admin routes aren't registered at all unless REKAI_ADMIN_KEY is
      // set server-side, so an unmatched route (not "unauthorized") means
      // this deployment simply doesn't have the admin API available.
      if (msg === "Not Found") {
        setNotConfigured(true);
      } else {
        setError(msg);
      }
      setKeys(null);
    }
  }, []);

  useEffect(() => {
    const stored = getStoredAdminKey();
    setAdminKey(stored);
    if (stored) load(stored);
  }, [load]);

  function save(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = adminKey.trim();
    setStoredAdminKey(trimmed);
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
    load(trimmed);
  }

  async function handleAdd(e: React.FormEvent) {
    e.preventDefault();
    if (!newKey.trim() || !adminKey) return;
    setBusy(true);
    setError("");
    setActionMsg("");
    try {
      const res = await addAdminKey(adminKey, newKey.trim());
      setActionMsg(`Added ${res.key}. Keep the raw key somewhere safe — it won't be shown again.`);
      setNewKey("");
      await load(adminKey);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to add key");
    } finally {
      setBusy(false);
    }
  }

  async function handleRevoke(e: React.FormEvent) {
    e.preventDefault();
    if (!revokeKeyValue.trim() || !adminKey) return;
    setBusy(true);
    setError("");
    setActionMsg("");
    try {
      const res = await revokeAdminKey(adminKey, revokeKeyValue.trim());
      setActionMsg(`Revoked ${res.key}.`);
      setRevokeKeyValue("");
      await load(adminKey);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to revoke key");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <h2>Admin</h2>
      <p className="hint">
        Add or revoke gateway API keys at runtime, without a redeploy (
        <code>REKAI_DYNAMIC_KEYS_ENABLED</code>). Requires a separate{" "}
        <code>REKAI_ADMIN_KEY</code> configured on the API — distinct from any gateway
        or provider key.
      </p>

      <form onSubmit={save}>
        <div className="field">
          <label htmlFor="adminKey">Admin key</label>
          <input
            id="adminKey"
            type="password"
            autoComplete="off"
            value={adminKey}
            placeholder="sk-rekai-admin-..."
            onChange={(e) => setAdminKey(e.target.value)}
          />
          <p className="hint">Stored only in your browser&apos;s local storage.</p>
        </div>

        <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 32 }}>
          <button type="submit">Save &amp; Refresh</button>
          {saved && <span className="saved">Saved ✓</span>}
        </div>
      </form>

      {notConfigured && (
        <div className="notice">
          This deployment doesn&apos;t have <code>REKAI_ADMIN_KEY</code> configured, so
          the admin API isn&apos;t available.
        </div>
      )}
      {error && <div className="error">{error}</div>}
      {actionMsg && <p className="saved">{actionMsg}</p>}

      {keys && (
        <>
          <div className="field">
            <label>Static keys (REKAI_API_KEYS)</label>
            {keys.static.length === 0 ? (
              <p className="hint">None configured.</p>
            ) : (
              <ul className="providers">
                {keys.static.map((k) => (
                  <li key={k}>
                    <span>{k}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="field">
            <label>Dynamic keys</label>
            {keys.dynamic.length === 0 ? (
              <p className="hint">None added yet.</p>
            ) : (
              <ul className="providers">
                {keys.dynamic.map((k) => (
                  <li key={k}>
                    <span>{k}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="field">
            <label htmlFor="newKey">Add a key</label>
            <form onSubmit={handleAdd} style={{ display: "flex", gap: 8 }}>
              <input
                id="newKey"
                value={newKey}
                placeholder="sk-rekai-new-tenant-key"
                onChange={(e) => setNewKey(e.target.value)}
              />
              <button type="submit" disabled={busy || !newKey.trim()}>
                Add
              </button>
            </form>
          </div>

          <div className="field">
            <label htmlFor="revokeKey">Revoke a key</label>
            <p className="hint">
              Only a key&apos;s masked form is ever shown above, so revoking needs the
              raw key — keep a record of it when you add one.
            </p>
            <form onSubmit={handleRevoke} style={{ display: "flex", gap: 8 }}>
              <input
                id="revokeKey"
                value={revokeKeyValue}
                placeholder="sk-rekai-key-to-revoke"
                onChange={(e) => setRevokeKeyValue(e.target.value)}
              />
              <button type="submit" disabled={busy || !revokeKeyValue.trim()}>
                Revoke
              </button>
            </form>
          </div>
        </>
      )}
    </div>
  );
}
