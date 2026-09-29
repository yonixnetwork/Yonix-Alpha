"use client";

import { Brain, Database, ShieldCheck } from "lucide-react";
import { Empty, ErrorNotice, Loading, Section, Stat } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
interface ModelReadiness {
  model: string; engines: string[]; feature_version: string; state: string; contributing: boolean; reason: string;
  effect: string; min_ml_confidence: number | null; drift_flag: boolean; drift: J | null;
  samples: { total: number; labeled: number; clean_labeled: number; quarantined: number; needed: number;
    first_at: string | null; last_at: string | null };
  validation: { holdout_size: number | null; auc: number | null; auc_lower_bound: number | null; brier: number | null;
    promotable: boolean | null };
  champion: J | null; challenger: J | null;
}
interface Readiness {
  models: ModelReadiness[]; states: string[]; ml_contributing: boolean;
  dataset: { decisions: number; rejected_or_not_traded: number; paper_trades: number; live_trades: number;
    manual_requests: number; completed_outcomes: number; labelled: number; missed_winners: number;
    first_decision_at: string | null; last_decision_at: string | null; last_shadow_training_at: string | null;
    shadow_models: { name: string; version: number; trained_at: string | null }[]; note: string };
}

const STATE_CLASS: Record<string, string> = {
  INSUFFICIENT_DATA: "pill pill-off", LEARNING: "pill pill-off", VALIDATING: "pill pill-warn", SHADOW: "pill pill-warn",
  PAPER_VALIDATED: "pill pill-ok", PRODUCTION_CONTRIBUTOR: "pill pill-ok",
};
const num = (v: number | null | undefined, d = 3) => (v === null || v === undefined ? "—" : v.toFixed(d));

/** Where each decision model is in its lifecycle, and whether ML touches any
 * decision right now. Read-only: nothing here trains or promotes. */
export default function MLReadiness() {
  const { data, error, loading } = useApi<Readiness>("/api/ml/readiness", undefined, { refreshMs: 60000 });
  return (
    <Section title="ML readiness">
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {data && (
        <>
          <div className="notice" role="status">
            {data.ml_contributing ? <Brain size={14} aria-hidden /> : <ShieldCheck size={14} aria-hidden />}{" "}
            {data.ml_contributing
              ? "ML contributes to decisions: a validated champion can make the gate WAIT. It never approves, sizes or overrides a safety rule."
              : "Decisions are rules only: no model contributes. ML contribution stays 0 until a model is validated, promoted by the operator and a minimum ML confidence is set."}
          </div>
          <div className="muted small" style={{ margin: "6px 0 12px" }}>Lifecycle: {data.states.join(" → ")}</div>
          {data.models.length === 0 ? <Empty>No decision models are defined.</Empty> : (
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr><th>Model</th><th>State</th><th>Why</th><th>Clean labelled / needed</th><th>Holdout AUC (lower bound)</th>
                  <th>Champion</th><th>Challenger</th><th>Drift</th></tr></thead>
                <tbody>{data.models.map((m) => (
                  <tr key={m.model}>
                    <td className="mono">{m.model}<div className="muted small">{m.engines.join(", ")}</div></td>
                    <td><span className={STATE_CLASS[m.state] ?? "pill pill-off"}>{m.state}</span>
                      <div className="muted small">{m.contributing ? "contributing" : "not contributing"}</div></td>
                    <td className="small" style={{ minWidth: 260 }}>{m.reason}<div className="muted small">effect: {m.effect}</div></td>
                    <td>{m.samples.clean_labeled} / {m.samples.needed}
                      <div className="muted small">{m.samples.total} snapshots · {m.samples.quarantined} quarantined</div></td>
                    <td>{num(m.validation.auc)} ({num(m.validation.auc_lower_bound)})
                      <div className="muted small">holdout {m.validation.holdout_size ?? "—"} · Brier {num(m.validation.brier)}</div></td>
                    <td>{m.champion ? `v${m.champion.version}` : "none"}
                      {m.champion?.activated_at && <div className="muted small">{formatDate(m.champion.activated_at)}</div>}</td>
                    <td>{m.challenger ? `v${m.challenger.version}` : "none"}
                      {m.challenger && <div className="muted small">{m.validation.promotable ? "promotable" : "not promotable"}</div>}</td>
                    <td>{m.drift_flag || m.drift?.status === "MODEL_DRIFT_DETECTED"
                      ? <span className="pill pill-danger">drift</span>
                      : m.drift?.status ?? <span className="muted">not measured</span>}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
          <div className="section-title" style={{ fontSize: 14, margin: "16px 0 8px" }}>
            <Database size={14} aria-hidden /> Learning dataset (every decision, traded or not)
          </div>
          <div className="stat-grid">
            <Stat label="Decisions recorded">{data.dataset.decisions}</Stat>
            <Stat label="Rejected / not traded">{data.dataset.rejected_or_not_traded}</Stat>
            <Stat label="Paper trades">{data.dataset.paper_trades}</Stat>
            <Stat label="Live trades">{data.dataset.live_trades}</Stat>
            <Stat label="Manual requests">{data.dataset.manual_requests}</Stat>
            <Stat label="Completed (tracked to T+60m)">{data.dataset.completed_outcomes}</Stat>
            <Stat label="Labelled">{data.dataset.labelled}</Stat>
            <Stat label="Missed winners">{data.dataset.missed_winners}</Stat>
            <Stat label="Last shadow training" hint={data.dataset.note}>{data.dataset.last_shadow_training_at ? formatDate(data.dataset.last_shadow_training_at) : "never"}</Stat>
          </div>
          <div className="muted small">
            Range: {data.dataset.first_decision_at ? formatDate(data.dataset.first_decision_at) : "—"} →{" "}
            {data.dataset.last_decision_at ? formatDate(data.dataset.last_decision_at) : "—"}
          </div>
        </>
      )}
    </Section>
  );
}
