"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AdminKeyList,
  ApiError,
  addAdminKey,
  fetchAdminKeys,
  getStoredAdminKey,
  grantAdminKeyCredit,
  revokeAdminKey,
  setStoredAdminKey,
} from "@/lib/api";

function formatCredit(v: number): string {
  if (v <= 0) return "credit exhausted";
  const wholeCents = Math.abs(v * 100 - Math.round(v * 100)) < 1e-9;
  return `$${v.toFixed(wholeCents ? 2 : 6)} credit`;
}

export default function AdminPage() {
  const [adminKey, setAdminKey] = useState("");
  const [saved, setSaved] = useState(false);
  const [keys, setKeys] = useState<AdminKeyList | null>(null);
  const [error, setError] = useState("");
  const [notConfigured, setNotConfigured] = useState(false);
  const [newKey, setNewKey] = useState("");
  const [newKeyTtl, setNewKeyTtl] = useState("");
  const [newKeyCredit, setNewKeyCredit] = useState("");
  const [revokeKeyValue, setRevokeKeyValue] = useState("");
  const [creditKeyValue, setCreditKeyValue] = useState("");
  const [creditAmountValue, setCreditAmountValue] = useState("");
  const [actionMsg, setActionMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (key: string) => {
    if (!key) return;
    setError("");
    setNotConfigured(false);
    try {
      setKeys(await fetchAdminKeys(key));
    } catch (e) {
      // The admin routes aren't registered at all unless REKAI_ADMIN_KEY is
      // set server-side, so a 404 (not a 401) means this deployment simply
      // doesn't have the admin API available. Branch on the status code, not
      // the error message text, which is a server-controlled string.
      if (e instanceof ApiError && e.status === 404) {
        setNotConfigured(true);
      } else {
        setError(e instanceof Error ? e.message : "Failed to load keys");
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
    setActionMsg("");
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
      const ttl = newKeyTtl.trim() === "" ? undefined : Number(newKeyTtl);
      const initialCredit =
        newKeyCredit.trim() === "" ? undefined : Number(newKeyCredit);
      const creditUsd =
        initialCredit !== undefined && Number.isFinite(initialCredit) && initialCredit > 0
          ? initialCredit
          : undefined;
      const res = await addAdminKey(
        adminKey,
        newKey.trim(),
        ttl !== undefined && Number.isFinite(ttl) && ttl > 0 ? ttl : undefined,
        creditUsd,
      );
      const creditSuffix =
        creditUsd === undefined ? "" : ` with $${creditUsd} credit`;
      setActionMsg(
        res.expires_at === null
          ? `Added ${res.key}${creditSuffix}. Keep the raw key somewhere safe — it won't be shown again.`
          : `Added ${res.key} (expires ${new Date(res.expires_at * 1000).toLocaleString()})${creditSuffix}. Keep the raw key somewhere safe — it won't be shown again.`,
      );
      setNewKey("");
      setNewKeyTtl("");
      setNewKeyCredit("");
      await load(adminKey);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to add key");
    } finally {
      setBusy(false);
    }
  }

  async function handleGrantCredit(e: React.FormEvent) {
    e.preventDefault();
    const amount = Number(creditAmountValue);
    if (
      !creditKeyValue.trim() ||
      !adminKey ||
      !creditAmountValue.trim() ||
      !Number.isFinite(amount) ||
      amount <= 0
    ) {
      return;
    }
    setBusy(true);
    setError("");
    setActionMsg("");
    try {
      const res = await grantAdminKeyCredit(adminKey, creditKeyValue.trim(), amount);
      setActionMsg(
        `Granted $${amount} to ${res.key}${
          res.credit_remaining_usd === null
            ? ""
            : ` — balance ${formatCredit(res.credit_remaining_usd)}`
        }`,
      );
      setCreditKeyValue("");
      setCreditAmountValue("");
      await load(adminKey);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to grant credit");
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
        or provider key. You can also sell prepaid USD credit per key; credited keys
        are metered at their real request cost.
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
                    {keys.credit_remaining_usd[k] !== undefined && (
                      <span className="hint"> — {formatCredit(keys.credit_remaining_usd[k])}</span>
                    )}
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
                    {keys.dynamic_expires_at[k] !== undefined && (
                      <span className="hint">
                        {" "}
                        — expires {new Date(keys.dynamic_expires_at[k] * 1000).toLocaleString()}
                      </span>
                    )}
                    {keys.credit_remaining_usd[k] !== undefined && (
                      <span className="hint"> — {formatCredit(keys.credit_remaining_usd[k])}</span>
                    )}
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
              <input
                value={newKeyTtl}
                placeholder="TTL seconds (blank = no expiry)"
                aria-label="Key TTL in seconds"
                inputMode="numeric"
                style={{ maxWidth: 220 }}
                onChange={(e) => setNewKeyTtl(e.target.value)}
              />
              <input
                value={newKeyCredit}
                placeholder="Credit USD (blank = unbilled)"
                aria-label="Initial credit in USD"
                inputMode="decimal"
                style={{ maxWidth: 200 }}
                onChange={(e) => setNewKeyCredit(e.target.value)}
              />
              <button type="submit" disabled={busy || !newKey.trim()}>
                Add
              </button>
            </form>
          </div>

          <div className="field">
            <label htmlFor="creditKey">Top up credit</label>
            <p className="hint">
              Works for static and runtime keys. Requests are debited at their real cost; a
              key at $0 is refused with 402 until topped up. Keys never given credit stay
              unbilled.
            </p>
            <form onSubmit={handleGrantCredit} style={{ display: "flex", gap: 8 }}>
              <input
                id="creditKey"
                value={creditKeyValue}
                placeholder="sk-rekai-key-to-top-up"
                onChange={(e) => setCreditKeyValue(e.target.value)}
              />
              <input
                id="creditAmount"
                value={creditAmountValue}
                placeholder="USD"
                inputMode="decimal"
                style={{ maxWidth: 140 }}
                onChange={(e) => setCreditAmountValue(e.target.value)}
              />
              <button
                type="submit"
                disabled={
                  busy ||
                  !creditKeyValue.trim() ||
                  !creditAmountValue.trim() ||
                  !Number.isFinite(Number(creditAmountValue)) ||
                  Number(creditAmountValue) <= 0
                }
              >
                Grant
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
