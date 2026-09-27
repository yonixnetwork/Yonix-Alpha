"use client";

import { useState } from "react";
import Link from "next/link";
import { ErrorNotice, Section, Stat, StatePill } from "@/components/ui";
import { apiPost, ApiError } from "@/lib/api";
import { useApi } from "@/lib/useApi";

type TestResult = { provider: string; status: string; detail: string; latency_ms: number };
type Group = {
  key: string; title: string; tests: string[];
  secrets: Record<string, string>; values: Record<string, string | boolean | null>; endpoints: Record<string, string | null>;
  last_test: Record<string, { status: string; detail: string; latency_ms: string; tested_at: string }>;
};
type Overview = { groups: Group[]; sections: { section: string; where: string; covers: string }[]; secret_update: string };

const LABEL: Record<string, string> = {
  solana_rpc: "Solana RPC", solana_ws: "Solana WebSocket", solana_rpc_backup: "Backup RPC",
  solana_rpc_backup_2: "Backup RPC 2", solana_rpc_backup_3: "Backup RPC 3", helius: "Helius key",
  jupiter: "Jupiter", pumpportal: "PumpPortal", binance: "Binance", bybit: "Bybit", hyperliquid: "Hyperliquid", mt5: "MT5 bridge",
  telegram: "Telegram",
};

function pill(status: string | undefined): string {
  if (!status) return "UNKNOWN";
  if (status === "CONNECTED") return "CONNECTED";
  if (status === "RATE LIMITED" || status === "TIMEOUT") return "DEGRADED";
  if (status === "INVALID CONFIGURATION") return "NOT CONFIGURED";
  return "UNAVAILABLE";
}

/** Every provider's configuration (secrets as configured / not configured
 * only) and a TEST CONNECTION button that makes one real read-only request
 * from the server. */
export default function ProvidersPanel() {
  const overview = useApi<Overview>("/api/settings/overview");
  const [results, setResults] = useState<Record<string, TestResult>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function test(name: string) {
    setBusy(name);
    setErr(null);
    try {
      const r = await apiPost<TestResult>(`/api/settings/providers/${name}/test`);
      setResults((prev) => ({ ...prev, [name]: r }));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const o = overview.data;
  return (
    <>
      <Section title="Providers & connections">
        <ErrorNotice error={overview.error ?? err} />
        <div className="notice">
          Secrets are never shown or sent to the browser — only whether they are configured. Provider keys are changed in
          “Change provider API keys” below; server-only secrets with <code>scripts/set-keys.sh</code>. TEST CONNECTION makes one real read-only request (a balance or
          account read, a quote, getSlot, a WebSocket subscription, Telegram getMe); it never places an order or sends a message.
        </div>
        {o?.groups.map((g) => (
          <div className="card" key={g.key}>
            <div className="status-label"><b>{g.title}</b></div>
            <div className="stat-grid">
              {Object.entries(g.secrets).map(([k, v]) => (
                <Stat key={k} label={k}><span className={v === "configured" ? "pill pill-ok" : "pill pill-off"}>{v}</span></Stat>
              ))}
              {Object.entries(g.values).map(([k, v]) => <Stat key={k} label={k}>{v === null || v === "" ? "—" : String(v)}</Stat>)}
              {Object.entries(g.endpoints).map(([k, v]) => <Stat key={k} label={k}>{v ?? "—"}</Stat>)}
            </div>
            {g.tests.length > 0 && (
              <div className="btn-row">
                {g.tests.map((t) => {
                  const r = results[t];
                  const last = g.last_test[t];
                  const status = r?.status ?? last?.status;
                  const detail = r?.detail ?? last?.detail;
                  return (
                    <div key={t} className="provider-test">
                      <button className="btn btn-sm" disabled={busy !== null} onClick={() => test(t)} aria-label={`Test ${LABEL[t] ?? t}`}>
                        {busy === t ? "Testing…" : `TEST ${LABEL[t] ?? t}`}
                      </button>{" "}
                      {status && <StatePill state={pill(status)} label={status} />}{" "}
                      {detail && <span className="muted">{detail}{r ? ` (${r.latency_ms} ms)` : ""}</span>}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        ))}
      </Section>
      {o && (
        <Section title="Settings center">
          <table className="data-table">
            <thead><tr><th>Section</th><th>What it covers</th><th>Where</th></tr></thead>
            <tbody>
              {o.sections.map((s) => (
                <tr key={s.section}>
                  <td>{s.section}</td><td>{s.covers}</td>
                  <td>{s.where.startsWith("/") ? <Link className="link" href={s.where}>{s.where}</Link> : <code>{s.where}</code>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      )}
    </>
  );
}
