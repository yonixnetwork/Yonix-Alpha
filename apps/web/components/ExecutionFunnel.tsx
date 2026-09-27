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
  pipeline?: Pipeline;
};
type FinalBlocker = { stage: string; code: string; reason?: string | null; groups?: string[] } | null;
type ExecutionTrail = { venue: string | null; venue_reason: string | null; provider: string | null; stage: string | null;
  stages: string[]; error: string | null } | null;
type PipelineToken = { mint: string; symbol: string | null; engine: string; stage: string; final_blocker: FinalBlocker;
  execution_route: string | null; promoted_at: string; execution?: ExecutionTrail };
type Pipeline = {
  stages: Record<string, number>; blocked_by: Record<string, number>;
  promoted_not_assessed: { engine: string; state: string; reason: string; tokens: number }[];
  final_blockers: { engine: string; stage: string; code: string; tokens: number }[];
  tokens: PipelineToken[];
};

const STAGE_LABEL: Record<string, string> = {
  OBSERVED: "Observed", ANALYSIS_POSITIVE: "Analysis positive", PROMOTE: "Promoted (to the gate)", BUY_SIGNAL: "BUY signal",
  RISK_APPROVED: "Risk approved", EXECUTION_APPROVED: "Execution approved", BUY_SUBMITTED: "Buy submitted",
  BUY_CONFIRMED: "Buy confirmed", POSITION_OPEN: "Position opened", SELL_SUBMITTED: "Sell submitted",
  SELL_CONFIRMED: "Sell confirmed", POSITION_CLOSED: "Position closed",
};
const GROUP_LABEL: Record<string, string> = {
  exit_signal_at_entry: "EXIT_SIGNAL_AT_ENTRY", liquidity: "Liquidity", sellability: "Sellability",
  stale_or_missing_data: "Stale / missing data", route: "Route", sizing_account: "Sizing / account limits", mode: "Mode",
  risk: "Risk (token, holders, flow, market)",
};

function stagePill(stage: string): string {
  if (["POSITION_OPEN", "SELL_SUBMITTED", "SELL_CONFIRMED", "POSITION_CLOSED", "BUY_CONFIRMED"].includes(stage)) return "pill pill-ok";
  if (["EXECUTION_APPROVED", "BUY_SUBMITTED", "RISK_APPROVED"].includes(stage)) return "pill pill-warn";
  return "pill pill-off";
}

function PipelineView({ pl }: { pl: Pipeline }) {
  return (
    <>
      <div className="status-label">Pipeline — where tokens stopped (PROMOTE is not a buy)</div>
      <table className="data-table">
        <thead><tr><th>Stage</th><th>Tokens</th></tr></thead>
        <tbody>{Object.entries(pl.stages).map(([s, n]) => (
          <tr key={s}><td>{STAGE_LABEL[s] ?? s}</td><td>{n}</td></tr>))}</tbody>
      </table>
      <div className="status-label">BUY signal but not executable — blocked by</div>
      <div className="stat-grid">
        {Object.entries(pl.blocked_by).map(([g, n]) => <Stat key={g} label={GROUP_LABEL[g] ?? g}>{n}</Stat>)}
      </div>
      {pl.promoted_not_assessed.length > 0 && (
        <>
          <div className="status-label">Promoted but never evaluated by the safety gate</div>
          <table className="data-table">
            <thead><tr><th>Engine</th><th>Candidate state</th><th>Recorded reason</th><th>Tokens</th></tr></thead>
            <tbody>{pl.promoted_not_assessed.map((r, i) => (
              <tr key={i}><td>{r.engine}</td><td>{r.state}</td><td className="muted">{r.reason}</td><td>{r.tokens}</td></tr>))}</tbody>
          </table>
        </>
      )}
      <div className="status-label">Latest tokens — furthest stage and exact final blocker</div>
      <table className="data-table">
        <thead><tr><th>Token</th><th>Engine</th><th>Stage</th><th>Final blocker</th><th>Route / execution</th></tr></thead>
        <tbody>{pl.tokens.slice(0, 40).map((tk) => (
          <tr key={`${tk.mint}-${tk.promoted_at}`}>
            <td><code>{tk.symbol ?? `${tk.mint.slice(0, 6)}…`}</code></td>
            <td>{ENGINE_LABEL[tk.engine] ?? tk.engine}</td>
            <td><span className={stagePill(tk.stage)}>{STAGE_LABEL[tk.stage] ?? tk.stage}</span></td>
            <td className="muted">{tk.final_blocker ? <><code>{tk.final_blocker.code}</code>{tk.final_blocker.reason ? ` — ${tk.final_blocker.reason}` : ""}</> : "—"}</td>
            <td>{tk.execution ? <>
              <b>{tk.execution.venue ?? "venue ?"}</b>{tk.execution.provider ? ` via ${tk.execution.provider}` : ""}
              <div className="muted" style={{ fontSize: "0.8em" }}>PROMOTE → {tk.execution.stages.join(" → ")}</div>
            </> : (tk.execution_route ?? "—")}</td>
          </tr>))}
          {pl.tokens.length === 0 && <tr><td colSpan={5} className="muted">No candidates in this window.</td></tr>}
        </tbody>
      </table>
    </>
  );
}

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
          {f.pipeline && <PipelineView pl={f.pipeline} />}
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
  pipeline?: { stage: string; final_blocker: FinalBlocker; execution_route?: string | null; execution_provider?: string | null };
  position_events?: { occurred_at: string; event_type: string;
    detail: { reasons?: string[]; reason?: string; error?: string } | null }[];
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
      {t.pipeline && (
        <div className="notice">
          Furthest stage: <span className={stagePill(t.pipeline.stage)}>{STAGE_LABEL[t.pipeline.stage] ?? t.pipeline.stage}</span>
          {t.pipeline.final_blocker && <> · stopped by <code>{t.pipeline.final_blocker.code}</code>
            {t.pipeline.final_blocker.reason ? ` — ${t.pipeline.final_blocker.reason}` : ""}</>}
          {t.pipeline.execution_route && <> · route {t.pipeline.execution_route} via {t.pipeline.execution_provider}</>}
        </div>
      )}
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
      {(t.position_events ?? []).map((e, i) => (
        <div key={`ev${i}`} className="muted">{formatDate(e.occurred_at)} <code>{e.event_type}</code>
          {(e.detail?.reasons ?? []).length ? ` · ${(e.detail?.reasons ?? []).join("; ")}` : e.detail?.reason ? ` · ${e.detail.reason}` : ""}</div>
      ))}
    </div>
  );
}
