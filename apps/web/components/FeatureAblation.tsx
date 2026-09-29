"use client";

import { FlaskConical } from "lucide-react";
import { Empty, ErrorNotice, Loading, Section } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const VERDICT: Record<string, string> = { ADDS_VALUE: "pill pill-ok", NO_EVIDENCE: "pill pill-off", HURTS: "pill pill-danger" };
const v = (x: unknown) => (x === null || x === undefined ? "—" : String(x));

/** Do the scanner-intelligence layers improve out-of-sample predictions?
 * A–F incremental sets, leave-one-out ablations and activity-matched
 * comparisons, as computed by the ml service. Review only. */
export default function FeatureAblation() {
  const { data, error, loading } = useApi<J>("/api/ml/ablation", undefined, { refreshMs: 300000 });
  return (
    <Section title="Feature ablation: do the scanner layers help?">
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {data && data.status !== "EVALUATED" && <Empty>{data.reason ?? data.status}</Empty>}
      {data?.status === "EVALUATED" && (
        <>
          <p className="muted small">
            <FlaskConical size={14} aria-hidden /> {data.samples} labelled decisions, {formatDate(data.period?.first)} →{" "}
            {formatDate(data.period?.last)} · train {data.split?.train} / holdout {data.split?.holdout} (forward in time, {data.split?.purged} purged) ·
            computed {formatDate(data.computed_at)} · {data.feature_version}. {data.note}
          </p>
          <p className="muted small">Feature availability in the holdout:{" "}
            {Object.entries(data.feature_availability?.holdout ?? {}).map(([k, x]) => `${k} ${Math.round(Number(x) * 100)}%`).join(" · ")}</p>
          {Object.entries(data.targets ?? {}).map(([target, t]: [string, any]) => (
            <div key={target} style={{ marginBottom: 16 }}>
              <h3 className="section-title" style={{ fontSize: 14 }}>{target}</h3>
              {t.status !== "EVALUATED" ? <p className="muted small">{t.status}: {t.reason}</p> : (
                <>
                  <div className="table-scroll">
                    <table className="data-table">
                      <thead><tr><th>Feature set</th><th>Features</th><th>ROC-AUC</th><th>PR-AUC</th><th>Brier</th><th>Calib. error</th>
                        <th>Precision</th><th>Recall</th><th>FP / FN</th><th>E[exec. return]</th><th>Drawdown</th><th>Missed winners</th></tr></thead>
                      <tbody>{Object.entries(t.sets).map(([name, m]: [string, any]) => (
                        <tr key={name}>
                          <td className="mono">{name}</td><td>{m.features}</td><td>{m.roc_auc}</td><td>{m.pr_auc}</td><td>{m.brier}</td>
                          <td>{m.calibration_error}</td><td>{v(m.decision?.precision)}</td><td>{v(m.decision?.recall)}</td>
                          <td>{m.decision?.false_positives} / {m.decision?.false_negatives}</td>
                          <td>{v(m.decision?.expected_executable_return_pct)}%</td><td>{v(m.decision?.mean_max_drawdown_pct)}%</td>
                          <td>{v(m.decision?.missed_winners)}</td>
                        </tr>
                      ))}</tbody>
                    </table>
                  </div>
                  <ul className="small">
                    {Object.entries(t.verdicts ?? {}).map(([k, x]: [string, any]) => (
                      <li key={k}><span className={VERDICT[x.verdict] ?? "pill pill-off"}>{x.verdict}</span> {k}: ΔAUC {x.delta_roc_auc}{" "}
                        (95% {x.delta_roc_auc_ci95 ? `${x.delta_roc_auc_ci95[0]} – ${x.delta_roc_auc_ci95[1]}` : "n/a"}) · ΔBrier {x.delta_brier}</li>
                    ))}
                  </ul>
                </>
              )}
            </div>
          ))}
          <h3 className="section-title" style={{ fontSize: 14 }}>Activity-matched comparison (treated − matched controls)</h3>
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>Signal</th><th>Treated (matched)</th><th>Fast dump</th><th>Rug</th><th>Upside 100%</th><th>Exec. return</th></tr></thead>
              <tbody>{Object.entries(data.matched ?? {}).map(([name, m]: [string, any]) => {
                const cell = (o: string) => {
                  const r = m.outcomes?.[o];
                  return r ? `${r.matched_difference} [${r.ci95[0]}, ${r.ci95[1]}] (raw ${v(r.raw_difference)})` : "—";
                };
                return (
                  <tr key={name}>
                    <td>{name}</td><td>{m.treated} ({m.matched_treated})</td><td className="small">{cell("fast_dump")}</td>
                    <td className="small">{cell("rug_60m")}</td><td className="small">{cell("upside_100")}</td>
                    <td className="small">{cell("executable_return_primary_pct")}</td>
                  </tr>
                );
              })}</tbody>
            </table>
          </div>
          <p className="muted small">Differences within comparable tokens (same engine, stage, regime, time of day, buyers, market cap, age,
            liquidity and recycled-wallet activity): associations, not causal effects.</p>
        </>
      )}
    </Section>
  );
}
