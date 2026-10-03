"use client";

import { useState } from "react";
import { Empty, ErrorNotice, Loading, Section, Stat } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const WHO: [string, string][] = [["deterministic", "Rules (trade signal)"], ["risk", "Risk layer"], ["final", "Final action"],
  ["ml", "ML (shadow)"]];
const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`);
const num = (v: number | null | undefined, d = 1) => (v === null || v === undefined ? "—" : v.toFixed(d));
const VERDICT_PILL: Record<string, string> = { BUY: "pill pill-ok", ALLOW: "pill pill-ok", WAIT: "pill pill-warn",
  REJECT: "pill pill-danger", IN_SAMPLE: "pill pill-off", NOT_AVAILABLE: "pill pill-off" };

/** EVM opportunities and wallet behaviour (master §36-44, §75): what the
 * models learned from, the BUY / WAIT / REJECT comparison (§41) and the
 * shadow models' out-of-sample metrics. ML contribution is 0 %. */
export default function EvmMlReview() {
  const [days, setDays] = useState(14);
  const { data, error, loading } = useApi<J>("/api/ml/evm", { days }, { refreshMs: 120000 });
  return (
    <Section title="EVM and wallet-behaviour ML (shadow)">
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {data && (
        <>
          <div className="btn-row">
            {[7, 14, 30].map((d) => (
              <button key={d} className={days === d ? "btn btn-sm" : "btn btn-ghost btn-sm"} onClick={() => setDays(d)}>{d} days</button>
            ))}
          </div>
          <div className="stat-grid">
            <Stat label="ML contribution" hint={data.contribution.why}>{data.contribution.percent}% ({data.contribution.status})</Stat>
            <Stat label="EVM samples (labelled)">{data.samples.evm_total} ({data.samples.evm_labelled})</Stat>
            <Stat label="By category">{Object.entries(data.samples.by_category as Record<string, number>).map(([k, v]) => `${k} ${v}`).join(" · ") || "—"}</Stat>
            <Stat label="Traded / rejected / expired">{data.samples.traded} / {data.samples.rejected} / {data.samples.expired_no_entry}</Stat>
            <Stat label="Wins / losses (traded, executable)">{data.samples.wins} / {data.samples.losses}</Stat>
            <Stat label="Missed winners" hint="not bought, then +100 % within the hour">{data.samples.missed_winners}</Stat>
            <Stat label="Scored (out of sample)">{data.samples.scored} ({data.samples.scored_out_of_sample})</Stat>
            <Stat label="Wallet episodes">{data.samples.wallet_episodes}</Stat>
            <Stat label="Copy outcomes">{data.samples.copy_outcomes}</Stat>
          </div>
          {Object.keys(data.samples.wallet_labels).length > 0 && (
            <p className="small">Wallet behaviour labels: {Object.entries(data.samples.wallet_labels as Record<string, number>)
              .map(([k, v]) => `${k.replaceAll("_", " ")} ${v}`).join(" · ")}</p>
          )}
          <h4>Decision comparison: BUY / WAIT / REJECT at the decision point, outcome in the hour after (§41)</h4>
          <p className="muted small">{data.comparison.note} Final action missed {data.comparison.final_missed_winners} winners and
            made {data.comparison.final_bad_entries} bad entries (bought, then -50 % within 10 min).</p>
          {data.samples.evm_total === 0 ? <Empty>No EVM samples yet: the first appear an hour after observations start (T+5 decision, 60 min outcome).</Empty> : (
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr><th>Recommender</th><th>Verdict</th><th>Samples</th><th>+50 % rate</th><th>+100 % rate</th><th>Fast dump rate</th><th>Mean 60 m return</th><th>Median</th><th>Executable (traded)</th></tr></thead>
                <tbody>{WHO.flatMap(([k, label]) => Object.entries((data.comparison[k] ?? {}) as Record<string, J>).map(([v, st], i) => (
                  <tr key={`${k}:${v}`}>
                    <td>{i === 0 ? label : ""}</td>
                    <td><span className={VERDICT_PILL[v] ?? "pill pill-off"}>{v.replaceAll("_", " ")}</span></td>
                    <td>{st.n}{st.labelled !== st.n ? <span className="muted small"> ({st.labelled} labelled)</span> : null}</td>
                    <td>{pct(st.upside_50_rate)}</td><td>{pct(st.upside_100_rate)}</td><td>{pct(st.fast_dump_rate)}</td>
                    <td className={st.mean_return_60m_pct > 0 ? "pos" : st.mean_return_60m_pct < 0 ? "neg" : ""}>{num(st.mean_return_60m_pct)}%</td>
                    <td>{num(st.median_return_60m_pct)}%</td>
                    <td>{st.executable ? `${num(st.executable.mean_pct)}% (n ${st.executable.n})` : "—"}</td>
                  </tr>
                )))}</tbody>
              </table>
            </div>
          )}
          {Object.keys(data.comparison.final_vs_ml).length > 0 && (
            <p className="small">Final action vs ML: {Object.entries(data.comparison.final_vs_ml as Record<string, number>).map(([k, v]) => `${k}: ${v}`).join(" · ")}</p>
          )}
          <h4>Shadow models</h4>
          {data.models.length === 0 ? <Empty>No EVM or wallet model yet: each needs 200 labelled samples.</Empty> : (
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr><th>Model</th><th>Target</th><th>Trained</th><th>Rows (train / holdout)</th><th>Holdout</th></tr></thead>
                <tbody>{(data.models as J[]).map((m) => (
                  <tr key={m.name}>
                    <td className="mono small">{m.name} v{m.version}</td><td>{m.target}</td><td className="small">{formatDate(m.trained_at)}</td>
                    <td>{m.split?.train ?? m.train_rows} / {m.split?.holdout ?? "—"}</td>
                    <td className="small">{m.kind === "binary"
                      ? `ROC-AUC ${m.holdout?.roc_auc ?? "—"} · PR-AUC ${m.holdout?.pr_auc ?? "—"} · Brier ${m.holdout?.brier ?? "—"} · base rate ${m.holdout?.base_rate ?? "—"}`
                      : `MAE ${m.holdout?.mae ?? "—"} vs ${m.holdout?.baseline_mae_train_mean ?? "—"} (mean baseline)${m.holdout?.beats_baseline ? "" : " · does not beat the baseline"}`}
                      {m.holdout?.warning && <div className="muted">{m.holdout.warning}</div>}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
          <p className="muted small">Decision point: {data.definitions.decision_point}. Labels: {data.definitions.labels}.</p>
        </>
      )}
    </Section>
  );
}
