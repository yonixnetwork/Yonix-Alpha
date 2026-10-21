"use client";

import Link from "next/link";
import { useState } from "react";
import { Empty, ErrorNotice, Loading, Section } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

interface Finding { code: string; kind: string; message: string }
interface Stage { stage: string; value: number | string | null; detail: Record<string, unknown> | null }
interface TrailRow {
  mint: string; symbol: string | null; state: string; promoted_at: string; stage: string;
  final_blocker: { code: string; message?: string } | null; assessments: number; execution_mode: string | null;
}
interface Pipeline {
  at: string; hours: number; mode: string; global_mode: string; verdict: string; findings: Finding[]; stages: Stage[];
  blockers: { code: string; category: string; n: number }[]; funnel_counters: string; last_decision_ever: string | null;
  trail?: TrailRow[];
}

const VERDICT: Record<string, string> = {
  A_NO_EVENTS: "A: no token events (scanner disconnected or stale)",
  B_NOT_PROCESSED: "B: events received but not processed",
  C_FILTERED: "C: tokens seen, rejected by filters",
  D_NOT_EVALUATED: "D: candidates did not reach the strategy evaluator",
  E_BLOCKED: "E: signals blocked by mode, risk or limits",
  F_EXECUTION_FAILED: "F: execution failed",
  G_OBSERVE_ONLY: "G: observing only (strategy OFF)",
  H_NO_SIGNAL: "H: scanner works, no candidate met the entry conditions",
  TRADING: "Trading: positions were opened",
  NO_EVIDENCE: "No evidence recorded in the window",
};
const KIND_CLASS: Record<string, string> = {
  SYSTEM: "pill pill-danger", WARNING: "pill pill-warn", CONFIGURATION: "pill pill-warn", BY_DESIGN: "pill pill-off",
  NO_OPPORTUNITY: "pill pill-off", OK: "pill pill-ok", UNKNOWN: "pill pill-warn",
};

function detailText(d: Record<string, unknown> | null): string {
  if (d === null) return "not recorded";
  return Object.entries(d).map(([k, v]) => `${k.replace(/_/g, " ")} ${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join(" · ");
}

/** Why momentum is or is not trading, from recorded evidence only
 * (GET /api/strategies/solana_momentum/pipeline): a broken scanner is
 * never shown as "no opportunity". */
export default function MomentumPipelinePanel() {
  const [hours, setHours] = useState(24);
  const [q, setQ] = useState("");
  const { data, error, loading } = useApi<Pipeline>("/api/strategies/solana_momentum/pipeline", { hours, tokens: 50 },
    { refreshMs: 60000, reloadOn: ["strategy.updated"] });
  const trail = (data?.trail ?? []).filter((t) => !q || t.mint.includes(q.trim()) || (t.symbol ?? "").toLowerCase().includes(q.trim().toLowerCase()));
  return (
    <Section title="Why momentum is or is not trading" actions={
      <select aria-label="Window" value={hours} onChange={(e) => setHours(Number(e.target.value))}>
        {[1, 6, 24, 48].map((h) => <option key={h} value={h}>last {h} h</option>)}
      </select>}>
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {data && (
        <>
          <div className="card" role="status">
            <b>{VERDICT[data.verdict] ?? data.verdict}</b>
            <ul className="reason-list" style={{ marginTop: 6 }}>
              {data.findings.map((f) => (
                <li key={f.code + f.message}><span className={KIND_CLASS[f.kind] ?? "pill"}>{f.kind.replace("_", " ")}</span> {f.message}</li>
              ))}
            </ul>
            <div className="muted small">Mode {data.mode} (global {data.global_mode}). Scan counters: {data.funnel_counters}.
              Last momentum gate decision ever: {data.last_decision_ever ? formatDate(data.last_decision_ever) : "never"}.
              Observation never authorizes a trade.</div>
          </div>
          <div className="table-wrap">
            <table className="data-table">
              <thead><tr><th>Stage</th><th>Count</th><th>Detail</th></tr></thead>
              <tbody>
                {data.stages.map((st) => (
                  <tr key={st.stage}><td>{st.stage}</td><td>{st.value ?? "—"}</td><td className="muted small">{detailText(st.detail)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
          {data.blockers.length > 0 && (
            <p className="muted small">Refusal reasons: {data.blockers.slice(0, 8).map((b) => `${b.code} ${b.n}`).join(", ")}</p>
          )}
          <h3 className="small">Candidate audit trail</h3>
          <input aria-label="Search mint or symbol" placeholder="search mint or symbol" value={q} onChange={(e) => setQ(e.target.value)} />
          {trail.length === 0 ? <Empty>{q ? "No momentum candidate matches." : "No momentum candidate in the window."}</Empty> : (
            <div className="table-wrap">
              <table className="data-table">
                <thead><tr><th>Promoted</th><th>Token</th><th>Stage reached</th><th>Stopped by</th><th>Gate evaluations</th></tr></thead>
                <tbody>
                  {trail.map((t) => (
                    <tr key={t.mint + t.promoted_at}>
                      <td className="muted small">{formatDate(t.promoted_at)}</td>
                      <td><Link className="link" href={`/dashboard/tokens/${t.mint}`}>{t.symbol || t.mint.slice(0, 8)}</Link></td>
                      <td>{t.stage}{t.execution_mode ? ` (${t.execution_mode})` : ""}</td>
                      <td className="small" title={t.final_blocker?.message}>{t.final_blocker?.code ?? "—"}</td>
                      <td>{t.assessments}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </Section>
  );
}
