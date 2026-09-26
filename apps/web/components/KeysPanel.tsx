"use client";

import { useEffect, useRef, useState } from "react";
import { ErrorNotice, Section } from "@/components/ui";
import { apiGet, apiPost, ApiError } from "@/lib/api";
import { useApi } from "@/lib/useApi";

type Key = { key: string; kind: "secret" | "url" | "text"; configured: boolean; value: string | null };
type Status = { installed: boolean; keys: Key[]; server_only: string[]; install: string };
type Result = { id: string; status: string; keys?: string[]; errors?: string[]; compose_exit_code?: number; backup?: string };

const POLL_MS = 3000;
const POLL_LIMIT = 80; // ~4 minutes: the api itself restarts while a change is applied

/** Write-only editor for provider API keys. New values go to the server
 * helper, which writes them into .env and restarts the affected services.
 * Current secret values are never sent to the browser. */
export default function KeysPanel() {
  const status = useApi<Status>("/api/settings/keys");
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [clear, setClear] = useState<Record<string, boolean>>({});
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  function poll(id: string, n = 0) {
    timer.current = setTimeout(async () => {
      try {
        const r = await apiGet<Result>(`/api/settings/keys/requests/${id}`);
        setResult(r);
        if (r.status === "QUEUED" || r.status === "UNKNOWN") {
          if (n < POLL_LIMIT) poll(id, n + 1);
        } else {
          status.reload();
        }
      } catch {
        // The api is being recreated with the new keys: keep polling.
        if (n < POLL_LIMIT) poll(id, n + 1);
      }
    }, POLL_MS);
  }

  const updates: Record<string, string> = {};
  for (const [k, v] of Object.entries(draft)) if (v.trim()) updates[k] = v.trim();
  for (const [k, on] of Object.entries(clear)) if (on) updates[k] = "";
  const count = Object.keys(updates).length;

  async function save() {
    setBusy(true);
    setErr(null);
    setResult(null);
    try {
      const r = await apiPost<{ request_id: string; keys: string[]; status: string }>("/api/settings/keys", { updates, password });
      setResult({ id: r.request_id, status: r.status, keys: r.keys });
      setDraft({});
      setClear({});
      setPassword("");
      poll(r.request_id);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const s = status.data;
  return (
    <Section title="Change provider API keys">
      <ErrorNotice error={status.error ?? err} />
      <div className="notice">
        New values are written into the server&apos;s <code>.env</code> by the server helper, which then restarts only the
        services that use them. Current values are never shown or sent to the browser. Wallet private keys, passwords,
        testnet flags and the trading locks can only be changed on the server (<code>scripts/set-keys.sh</code>).
      </div>
      {s && !s.installed && (
        <div className="notice notice-danger">
          The key updater is not installed on this server yet. Run once, as root, in /opt/yonixalpha:{" "}
          <code>scripts/install-env-updater.sh</code>
        </div>
      )}
      {s && (
        <>
          <table className="data-table">
            <thead><tr><th>Key</th><th>Now</th><th>New value</th><th>Clear</th></tr></thead>
            <tbody>
              {s.keys.map((k) => (
                <tr key={k.key}>
                  <td><code>{k.key}</code></td>
                  <td>
                    <span className={k.configured ? "pill pill-ok" : "pill pill-off"}>{k.configured ? "configured" : "not configured"}</span>
                    {k.value ? <span className="muted"> {k.value}</span> : null}
                  </td>
                  <td>
                    <label className="sr-only" htmlFor={`key-${k.key}`}>New value for {k.key}</label>
                    <input
                      id={`key-${k.key}`}
                      type={k.kind === "secret" ? "password" : "text"}
                      autoComplete="off"
                      spellCheck={false}
                      placeholder={k.kind === "url" ? "https://… or wss://…" : "leave empty to keep"}
                      value={draft[k.key] ?? ""}
                      disabled={!s.installed || clear[k.key]}
                      onChange={(e) => setDraft({ ...draft, [k.key]: e.target.value })}
                    />
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Clear ${k.key}`}
                      checked={!!clear[k.key]}
                      disabled={!s.installed || !k.configured}
                      onChange={(e) => setClear({ ...clear, [k.key]: e.target.checked })}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="inline-form" style={{ marginTop: 12 }}>
            <label className="sr-only" htmlFor="keys-password">Admin password</label>
            <input
              id="keys-password"
              type="password"
              autoComplete="current-password"
              placeholder="Admin password (required)"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <button className="btn" disabled={!s.installed || busy || count === 0 || !password} onClick={save}>
              {busy ? "Saving…" : `Save ${count} change${count === 1 ? "" : "s"} and restart services`}
            </button>
          </div>
        </>
      )}
      {result && (
        <div className={result.status.startsWith("APPLIED") && !result.status.includes("FAILED") ? "success" : result.status === "QUEUED" || result.status === "UNKNOWN" ? "notice" : "error"}
          aria-live="polite">
          {result.status === "QUEUED" || result.status === "UNKNOWN"
            ? `Queued ${result.keys?.join(", ") ?? ""} — waiting for the server helper (services restart; this page may briefly lose connection)…`
            : `${result.status}: ${result.keys?.join(", ") ?? ""}`}
          {result.errors?.length ? ` — ${result.errors.join("; ")}` : ""}
          {result.status.startsWith("APPLIED") ? " — use TEST on the provider above to confirm the new key works." : ""}
        </div>
      )}
    </Section>
  );
}
