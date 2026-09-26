"use client";

import { useState } from "react";
import { ErrorNotice, Section, Stat } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type Stage = { assessments: number; tokens: number; signal_tokens: number; executable_tokens: number;
  executable_paper: number; executable_live: number };
type Block = { engine: string; code: string; decision?: string; action?: string; tokens: number; assessments: number; example?: string };
type Funnel = {
  since: string; observed: Record<string, number>; stages: Record<string, Stage>;
  blocked_with_buy_signal: Block[]; approval_drivers: Block[]; near_misses: Block[];
  positions: Record<string, Record<string, Record<string, number>>>; orders: Record<string, Record<string, number>>;
  order_errors: { side: string; error: string; n: number }[];
  execution_failures: { event_type: string; reason: string; n: number }[];
  modes: { global: string; strategies: Record<string, string>; live_ready?: boolean; live_not_ready_reason?: string | null };
  diagnosis: string[];
};

const ENGINE_LABEL: Record<string, string> = { solana_fresh: "Fresh (curve)", solana_momentum: "Momentum (curve)",
  solana_migration: "Migrated (PumpSwap)" };

function positionsOf(p: Funnel["positions"][string] | undefined): string {
  if (!p) return "0";
  return Object.entries(p).map(([mode, st]) => `${mode} ${Object.values(st).reduce((a, b) => a + b, 0)}`).join(", ");
}

/** Where every token stopped: observed → BUY signal → executable → position
 * → live order, with the exact codes that blocked it. Counted from the
 * database; nothing here is estimated. */
export default function ExecutionFunnel() {
  const [hours, setHours] = useState(24);
  const { data: f, error } = useApi<Funnel>("/api/control/execution-funnel", { hours }, { refreshMs: 30000 });
  return (
    <Section title="Execution funnel — why no buy?"
      actions={
        <div className="btn-row">
          <label className="sr-only" htmlFor="funnel-hours">Window</label>
          <select id="funnel-hours" value={hours} onChange={(e) => setHours(Number(e.target.value))}>
            {[1, 6, 24, 72].map((h) => <option key={h} value={h}>last {h} h</option>)}
          </select>
        </div>
      }>
      <ErrorNotice error={error} />
      {f && (
        <>
          <div className="stat-grid">
            <Stat label="Global mode">{f.modes.global}</Stat>
            {Object.entries(f.modes.strategies).map(([k, v]) => <Stat key={k} label={`${ENGINE_LABEL[k] ?? k} mode`}>{v}</Stat>)}
            {f.modes.global === "LIVE" && (
              <Stat label="Live execution">{f.modes.live_ready ? "READY" : `NOT READY — ${f.modes.live_not_ready_reason ?? ""}`}</Stat>
            )}
          </div>
          {f.diagnosis.length > 0 && (
            <div className="notice" aria-live="polite">
              <b>Diagnosis</b>
              <ul className="reason-list">{f.diagnosis.map((d, i) => <li key={i}>{d}</li>)}</ul>
            </div>
          )}
          <table className="data-table">
            <thead><tr><th>Engine</th><th>Assessed</th><th>BUY signal</th><th>Risk: executable</th><th>Positions opened</th></tr></thead>
            <tbody>
              {Object.entries(f.stages).map(([e, s]) => (
                <tr key={e}>
                  <td>{ENGINE_LABEL[e] ?? e}</td>
                  <td>{s.tokens} tokens <span className="muted">({s.assessments} evaluations)</span></td>
                  <td>{s.signal_tokens}</td>
                  <td>{s.executable_tokens} <span className="muted">(paper {s.executable_paper}, live {s.executable_live})</span></td>
                  <td>{positionsOf(f.positions[e])}</td>
                </tr>
              ))}
              {Object.keys(f.stages).length === 0 && <tr><td colSpan={5} className="muted">No gate evaluations in this window.</td></tr>}
            </tbody>
          </table>
          {f.blocked_with_buy_signal.length > 0 && (
            <>
              <div className="status-label">Had a BUY signal, blocked by</div>
              <table className="data-table">
                <thead><tr><th>Engine</th><th>Code</th><th>Decision</th><th>Tokens</th><th>Example</th></tr></thead>
                <tbody>{f.blocked_with_buy_signal.slice(0, 12).map((b, i) => (
                  <tr key={i}><td>{ENGINE_LABEL[b.engine] ?? b.engine}</td><td><code>{b.code}</code></td><td>{b.decision}</td>
                    <td>{b.tokens}</td><td className="muted">{b.example}</td></tr>))}</tbody>
              </table>
            </>
          )}
          {f.approval_drivers.length > 0 && (
            <>
              <div className="status-label">Behind “needs approval” (AUTO turns it into NO_TRADE)</div>
              <table className="data-table">
                <thead><tr><th>Engine</th><th>HIGH finding</th><th>Its own action</th><th>Tokens</th></tr></thead>
                <tbody>{f.approval_drivers.slice(0, 10).map((b, i) => (
                  <tr key={i}><td>{ENGINE_LABEL[b.engine] ?? b.engine}</td><td><code>{b.code}</code></td><td>{b.action}</td><td>{b.tokens}</td></tr>))}</tbody>
              </table>
            </>
          )}
          {(Object.keys(f.orders).length > 0 || f.execution_failures.length > 0) && (
            <>
              <div className="status-label">Execution</div>
              <div className="stat-grid">
                {Object.entries(f.orders).map(([side, st]) => (
                  <Stat key={side} label={`live ${side}`}>{Object.entries(st).map(([k, v]) => `${k} ${v}`).join(", ")}</Stat>
                ))}
              </div>
              <ul className="reason-list">
                {f.order_errors.map((e, i) => <li key={`o${i}`}>{e.side} order error ×{e.n}: {e.error}</li>)}
                {f.execution_failures.map((e, i) => <li key={`e${i}`}><code>{e.event_type}</code> ×{e.n}: {e.reason}</li>)}
              </ul>
            </>
          )}
          <div className="muted">Since {formatDate(f.since)}. Server command: <code>python -m yonixalpha_core.tools.execution_funnel</code></div>
        </>
      )}
    </Section>
  );
}

type Trace = {
  observation: { outcome: string; trend: string | null }[];
  assessments: { evaluated_at: string; engine: string; decision: string; executable: boolean; execution_target: string;
    buy_signal: boolean; size: string | null; blocking: { code: string; message: string }[] | null }[];
  positions: { execution_mode: string; status: string; entry_at: string; lifecycle: string | null; exit_reason: string | null;
    realized_pnl: string | null }[];
  orders: { side: string; status: string; signature: string | null; error: string | null; created_at: string }[];
};

function executionState(t: Trace): { label: string; cls: string } {
  const lastOrder = t.orders[t.orders.length - 1];
  if (lastOrder) {
    const map: Record<string, string> = { PENDING: "ENTRY_PENDING", SIGNED: "BUY_SUBMITTED", SUBMITTED: "BUY_SUBMITTED",
      CONFIRMED: "BUY_CONFIRMED", FAILED: "BUY_FAILED", EXPIRED: "BUY_FAILED", CANCELLED: "EXECUTION_FAILED" };
    const s = map[lastOrder.status] ?? lastOrder.status;
    const label = lastOrder.side === "SELL" ? s.replace("BUY_", "SELL_").replace("ENTRY_PENDING", "EXIT_PENDING") : s;
    return { label, cls: label.includes("FAILED") ? "pill pill-danger" : label.includes("CONFIRMED") ? "pill pill-ok" : "pill pill-warn" };
  }
  const last = t.assessments[t.assessments.length - 1];
  if (!last) return { label: "NOT EVALUATED", cls: "pill pill-off" };
  if (last.executable) return { label: t.positions.length ? "EXECUTED" : "READY", cls: "pill pill-ok" };
  return { label: `BLOCKED — ${(last.blocking ?? []).map((b) => b.code).slice(0, 2).join(", ") || last.decision}`, cls: "pill pill-danger" };
}

/** ACTIVITY is what the stream saw; SIGNAL, RISK and EXECUTION are what the
 * engine decided — "buyers increasing" alone never authorises a buy. */
export function TokenPipeline({ mint }: { mint: string }) {
  const { data: t, error } = useApi<Trace>(`/api/control/execution-funnel/token/${encodeURIComponent(mint)}`);
  if (error) return <ErrorNotice error={error} />;
  if (!t) return null;
  const last = t.assessments[t.assessments.length - 1];
  const pos = t.positions[t.positions.length - 1];
  const exec = executionState(t);
  return (
    <div className="card">
      <div className="stat-grid">
        <Stat label="Activity">{t.observation[0]?.trend ?? "—"}</Stat>
        <Stat label="Signal">{last ? (last.buy_signal ? "BUY" : "NO BUY SIGNAL") : "—"}</Stat>
        <Stat label="Risk">{last ? (last.executable ? "APPROVED" : last.decision) : "—"}</Stat>
        <Stat label="Execution"><span className={exec.cls}>{exec.label}</span></Stat>
        <Stat label="Position">{pos ? `${pos.execution_mode} ${pos.status.toUpperCase()}${pos.lifecycle ? ` · ${pos.lifecycle}` : ""}` : "NOT ENTERED"}</Stat>
      </div>
      {t.assessments.length > 0 && (
        <table className="data-table">
          <thead><tr><th>Evaluated</th><th>Engine</th><th>Signal</th><th>Decision</th><th>Size</th><th>Blocking reason</th></tr></thead>
          <tbody>{t.assessments.slice(-15).map((a, i) => (
            <tr key={i}>
              <td>{formatDate(a.evaluated_at)}</td><td>{a.engine}</td><td>{a.buy_signal ? "BUY" : "—"}</td>
              <td>{a.decision}{a.executable ? ` → ${a.execution_target}` : ""}</td><td>{a.size ?? "—"}</td>
              <td className="muted">{(a.blocking ?? []).map((b) => `${b.code}: ${b.message}`).join(" · ") || "—"}</td>
            </tr>))}</tbody>
        </table>
      )}
      {t.orders.map((o, i) => (
        <div key={i} className="muted">{formatDate(o.created_at)} {o.side} {o.status}{o.signature ? ` · ${o.signature}` : ""}{o.error ? ` · ${o.error}` : ""}</div>
      ))}
    </div>
  );
}
