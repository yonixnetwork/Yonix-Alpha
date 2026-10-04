"use client";

import { Fragment } from "react";
import { Gauge } from "lucide-react";
import ConfirmButton from "@/components/ConfirmDialog";
import EvmMlReview from "@/components/EvmMlReview";
import MlSteps from "@/components/MlSteps";
import FeatureAblation from "@/components/FeatureAblation";
import LedgerReview from "@/components/LedgerReview";
import OpportunityOutcomes from "@/components/OpportunityOutcomes";
import { Empty, ErrorNotice, Loading, PageHeader, Section, Stat } from "@/components/ui";
import { apiPost } from "@/lib/api";
import type { ModelReview, ModelSummary } from "@/lib/cc";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

const METRICS: [string, string][] = [
  ["auc", "AUC"],
  ["auc_lower_bound", "AUC lower bound (95%)"],
  ["brier", "Brier (calibration)"],
  ["precision", "Precision"],
  ["recall", "Recall"],
  ["false_positive_rate", "False-positive rate"],
  ["false_negative_rate", "False-negative rate"],
  ["stability_auc_gap", "Stability gap (holdout halves)"],
  ["paper_return_favoured_avg", "Avg paper return, favoured"],
  ["paper_return_all_avg", "Avg paper return, all"],
  ["holdout_size", "Holdout size"],
];

const fmt = (v: unknown) => (typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toFixed(4)) : v === null || v === undefined ? "—" : String(v));

function Compare({ challenger, champion }: { challenger: ModelSummary | null; champion: ModelSummary | null }) {
  const c = challenger?.metrics?.challenger ?? {};
  // The challenger's evaluation also scored the champion on the same holdout.
  const ch = challenger?.metrics?.champion ?? {};
  return (
    <div className="table-wrap">
      <table className="data-table">
        <caption className="table-caption">Evaluated on the same forward-in-time holdout</caption>
        <thead>
          <tr>
            <th>Metric</th>
            <th>Challenger {challenger ? `v${challenger.version}` : ""}</th>
            <th>Champion {champion ? `v${champion.version}` : "(none)"}</th>
          </tr>
        </thead>
        <tbody>
          {METRICS.map(([k, label]) => (
            <tr key={k}>
              <td>{label}</td>
              <td>{fmt(c[k])}</td>
              <td>{fmt(ch[k])}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ModelCard({ m, reload }: { m: ModelReview; reload: () => void }) {
  const drift = m.drift;
  return (
    <div className="card">
      <div className="status-label">
        {m.model} · engines {m.engines.join(", ")} · features {m.feature_version}
      </div>
      <div className={m.mode.startsWith("ML IGNORED") ? "notice notice-warn" : "notice"}>
        Decision mode: <b>{m.mode}</b>. ML can only add caution (a WAIT when confidence is below the minimum you set); it
        never overrides a risk control.
      </div>
      <div className="stat-grid">
        <Stat label="Samples (this feature version)">{m.samples.total}</Stat>
        <Stat label="Labeled">{m.samples.labeled}</Stat>
        <Stat label="Passed quality">{m.samples.quality_ok}</Stat>
        <Stat label="Quarantined">{m.samples.quarantined}</Stat>
        <Stat label="Scored by champion">{m.samples.scored}</Stat>
        <Stat label="Champion">{m.champion ? `v${m.champion.version} since ${formatDate(m.champion.activated_at)}` : "none — rules only"}</Stat>
      </div>
      {!m.challenger && !m.champion && (
        <Empty>No model trained yet: training starts once 50 clean, labeled, closed paper trades exist for these engines.</Empty>
      )}
      {m.challenger && (
        <>
          <Compare challenger={m.challenger} champion={m.champion} />
          <div className={m.challenger.metrics.promotable ? "success" : "notice notice-warn"}>{String(m.challenger.metrics.promotion_note ?? "")}</div>
          <div className="btn-row" style={{ marginTop: 8 }}>
            <ConfirmButton
              label={`Promote v${m.challenger.version}`}
              disabled={!m.challenger.metrics.promotable}
              title={`Promote ${m.model} v${m.challenger.version} to champion?`}
              body="The decision engine starts scoring with it on the next cycle. The change is audited, and you can retire it at any time."
              onConfirm={async () => {
                await apiPost(`/api/ml/models/${m.challenger!.id}/promote`, { note: "promoted from ML Review" });
                reload();
              }}
            />
          </div>
        </>
      )}
      {m.champion && (
        <>
          <div className="section-title">Drift</div>
          {drift ? (
            <dl className="kv">
              <dt>Status</dt>
              <dd className={drift.status === "ok" ? "level-LOW" : "level-HIGH"}>{drift.status}</dd>
              <dt>Checked</dt>
              <dd>{formatDate(drift.checked_at)}</dd>
              <dt>Prediction PSI</dt>
              <dd>{fmt(drift.prediction_psi)}</dd>
              {Object.entries(drift.feature_psi ?? {}).map(([k, v]) => (
                <Fragment key={k}>
                  <dt>PSI {k}</dt>
                  <dd>{fmt(v)}</dd>
                </Fragment>
              ))}
              <dt>Recent accuracy (drop)</dt>
              <dd>
                {fmt(drift.recent_accuracy)} ({fmt(drift.accuracy_drop)})
              </dd>
            </dl>
          ) : (
            <div className="muted">Not checked yet (needs 20 recent decisions).</div>
          )}
          <div className="btn-row" style={{ marginTop: 8 }}>
            <ConfirmButton
              label={`Retire v${m.champion.version}`}
              danger
              title={`Retire ${m.model} v${m.champion.version}?`}
              body="Decisions for these engines go back to rules only until another challenger is promoted."
              onConfirm={async () => {
                await apiPost(`/api/ml/${m.model}/retire`, { reason: "retired from ML Review" });
                reload();
              }}
            />
          </div>
        </>
      )}
    </div>
  );
}

export default function MLReviewPage() {
  const review = useApi<ModelReview[]>("/api/ml/review", undefined, { reloadOn: ["ml.model.updated"], refreshMs: 60000 });
  const preds = useApi<any[]>("/api/ml/predictions", { limit: 50 }, { reloadOn: ["ml.prediction.updated"] });
  const quality = useApi<Record<string, any>>("/api/ml/data-quality", { limit: 50 });
  return (
    <div>
      <PageHeader title="ML Review" icon={<Gauge size={20} aria-hidden />} subtitle="Champion / challenger, promotion, drift and data quality. Nothing is promoted automatically." />
      <ErrorNotice error={review.error} />
      {review.loading && !review.data && <Loading />}
      {review.data?.map((m) => <ModelCard key={m.model} m={m} reload={review.reload} />)}
      <FeatureAblation />
      <Section title="Predictions vs outcomes">
        {preds.data && preds.data.length === 0 ? (
          <Empty>No scored decisions yet.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Model</th>
                  <th>Symbol</th>
                  <th>Score</th>
                  <th>Outcome</th>
                </tr>
              </thead>
              <tbody>
                {preds.data?.map((p) => (
                  <tr key={p.id}>
                    <td className="muted">{formatDate(p.at)}</td>
                    <td>
                      {p.model} v{p.version}
                    </td>
                    <td>{p.symbol}</td>
                    <td>{fmt(Number(p.score))}</td>
                    <td>{p.label === null ? <span className="muted">open / unlabeled</span> : p.label ? "profitable" : "not profitable"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
      <Section title="Data quality (quarantined samples)">
        {quality.data && (
          <>
            <div className="muted">
              {quality.data.total} event(s):{" "}
              {Object.entries(quality.data.by_issue ?? {})
                .map(([k, v]) => `${k} ${v}`)
                .join(", ") || "none"}
            </div>
            {quality.data.items.length > 0 && (
              <ul className="reason-list">
                {quality.data.items.slice(0, 20).map((e: any) => (
                  <li key={e.id}>
                    <b>{e.issue}</b> · {e.record_type} {String(e.record_id).slice(0, 8)} · {formatDate(e.at)}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </Section>
      <LedgerReview />
      <OpportunityOutcomes />
      <MlSteps />
      <EvmMlReview />
    </div>
  );
}
