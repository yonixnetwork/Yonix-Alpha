"use client";

import { useState } from "react";
import ConfirmButton from "@/components/ConfirmDialog";
import { Empty, ErrorNotice, Section } from "@/components/ui";
import { apiPost, apiPut } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const HEALTH: Record<string, string> = {
  OK: "pill pill-ok", DEGRADED: "pill pill-danger", DRIFT: "pill pill-danger", NOT_VALIDATED: "pill pill-off",
  NOT_APPLICABLE: "pill pill-off",
};
const VCLASS: Record<string, string> = { PASS: "pill pill-ok", FAIL: "pill pill-danger", INSUFFICIENT_DATA: "pill pill-off" };
const num = (v: unknown, d = 3) => (typeof v === "number" ? v.toFixed(d) : "NOT AVAILABLE");

/** The contribution controls of a model that has a decision consumer:
 * stage and percentage, refused by the API (with its reasons) unless the
 * governance rules allow the change. */
function ContributionEditor({ m, rules, reload }: { m: J; rules: J; reload: () => void }) {
  const [stage, setStage] = useState<string>(m.stage);
  const [pct, setPct] = useState<number>(m.percent);
  const steps: number[] = [];
  for (let p = 0; p <= rules.max_pct; p += rules.step_pct) steps.push(p);
  const changed = stage !== m.stage || pct !== m.percent;
  return (
    <div className="btn-row">
      <label className="small">Stage{" "}
        <select value={stage} onChange={(e) => { setStage(e.target.value); if (e.target.value !== "PAPER_CONTRIBUTOR") setPct(0); }}>
          {["OBSERVATION_ONLY", "SHADOW", "PAPER_CONTRIBUTOR"].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </label>
      <label className="small">Contribution{" "}
        <select value={pct} disabled={stage !== "PAPER_CONTRIBUTOR"} onChange={(e) => setPct(Number(e.target.value))}>
          {steps.map((p) => <option key={p} value={p}>{p} %</option>)}
        </select>
      </label>
      <ConfirmButton
        label="Apply"
        disabled={!changed}
        title={`Set ${m.name} to ${stage} at ${pct} %?`}
        body={<>
          <p>Raising needs the operator-promoted champion to have a PASS on its frozen validation set, goes {rules.step_pct} % at
            a time, and at most once every {rules.min_days_between_increases} days. Lowering is always allowed.</p>
          <p className="muted small">The change is audited. It affects PAPER decisions only; ML never overrides a safety rule.</p>
        </>}
        onConfirm={async () => { await apiPut(`/api/ml/governance/${m.name}`, { stage, percent: pct }); reload(); }}
      />
    </div>
  );
}

/** Master §38-42: every model's stage, contribution, validation on frozen
 * sets it never saw, and health. */
export default function MlGovernance() {
  const { data, error, reload } = useApi<J>("/api/ml/governance", undefined, { refreshMs: 60000 });
  if (error) return <ErrorNotice error={error} />;
  if (!data) return null;
  const models: J[] = data.models;
  return (
    <Section title="ML governance">
      <p className="muted small">{data.note}</p>
      <p className="muted small">{data.rules.pass_rule}. Frozen sets: {data.rules.freeze}. {data.rules.live}.</p>
      <div className="table-scroll"><table className="data-table">
        <thead><tr><th>Model</th><th>Role</th><th>Stage</th><th>Contribution</th><th>Version</th><th>Training samples</th>
          <th>Validation samples</th><th>Out-of-sample AUC</th><th>Confidence (AUC lower bound)</th><th>Calibration (ECE)</th>
          <th>Frozen-set verdict</th><th>Health</th></tr></thead>
        <tbody>{models.map((m) => {
          const v = m.validation;
          return (
            <tr key={m.name}>
              <td><code>{m.name}</code><div className="muted small">{m.family}{m.target ? ` / ${m.target}` : ""}</div></td>
              <td className="small">{m.kind}<div className="muted small">{m.consumer}</div></td>
              <td><span className="pill pill-off">{m.stage}</span></td>
              <td>{m.kind === "contributor" ? `${m.percent} %` : "0 %"}</td>
              <td>{m.version === null ? "none" : `v${m.version} (${m.status})`}
                {m.champion_version !== null && m.champion_version !== m.version
                  ? <div className="muted small">champion v{m.champion_version}</div> : null}</td>
              <td>{m.training_samples ?? "NOT AVAILABLE"}</td>
              <td>{m.validation_samples ?? "NOT AVAILABLE"}</td>
              <td>{num(m.out_of_sample_auc)}</td>
              <td>{num(m.confidence_auc_lower_bound)}</td>
              <td>{num(m.calibration_ece)}</td>
              <td>{v ? <><span className={VCLASS[v.status] ?? "pill pill-off"}>{v.status}</span>
                <div className="muted small">{v.reason}</div></> : <span className="muted small">no report yet</span>}</td>
              <td><span className={HEALTH[m.health] ?? "pill pill-off"}>{m.health}</span>
                <div className="muted small">{m.health_reason}</div></td>
            </tr>);
        })}</tbody>
      </table></div>

      {models.filter((m) => m.kind === "contributor").map((m) => (
        <div key={m.name} className="card" style={{ marginTop: 12 }}>
          <div className="section-title">{m.name}: contribution</div>
          <p className="small">Consumer: {m.consumer}. Now {m.stage} at {m.percent} % (weight {m.weight}).
            {m.changed_at ? ` Last change ${formatDate(m.changed_at)} by ${m.changed_by}.` : " Never changed."}
            {m.raised_at ? ` Last raise ${formatDate(m.raised_at)}.` : ""}</p>
          {m.challenger && (
            <div className="btn-row">
              <ConfirmButton
                label={`Promote challenger v${m.challenger.version}`}
                disabled={!m.challenger.promotable}
                title={`Promote ${m.name} v${m.challenger.version} to champion?`}
                body="Promotion only makes it the champion. Its contribution stays where it is (0 % until you raise it after a frozen-set PASS). Audited."
                onConfirm={async () => { await apiPost(`/api/ml/models/${m.challenger.id}/promote`, { note: "promoted from ML governance" }); reload(); }}
              />
              {!m.challenger.promotable && <span className="muted small">the challenger did not pass the holdout bar</span>}
            </div>
          )}
          <ContributionEditor key={`${m.stage}-${m.percent}`} m={m} rules={data.rules} reload={reload} />
          {m.validation?.windows?.length ? (
            <p className="muted small">Validated on frozen windows: {m.validation.windows.map((w: J) =>
              `${w.window} (${w.n} samples${typeof w.auc === "number" ? `, AUC ${w.auc.toFixed(3)}` : ""})`).join(", ")}.</p>
          ) : null}
        </div>
      ))}

      {models.filter((m) => m.kind === "shadow" && m.validation?.decisions).map((m) => {
        const d = m.validation.decisions;
        return (
          <div key={`d-${m.name}`} className="card" style={{ marginTop: 12 }}>
            <div className="section-title">{m.name}: decisions on the frozen set</div>
            {d.ml_buy && d.rules_final_buy ? (
              <table className="data-table"><thead><tr><th>Group</th><th>n</th><th>Mean 60 min return</th>
                <th>Executable return</th><th>Max drawdown</th><th>Fast dump rate</th></tr></thead>
              <tbody>{[["ML BUY", d.ml_buy], ["Rules final BUY", d.rules_final_buy]].map(([k, g]: J[]) => (
                <tr key={String(k)}><td>{String(k)}</td><td>{g.n}</td><td>{num(g.mean_return_60m_pct, 1)}</td>
                  <td>{g.executable?.n ? `${num(g.executable.mean_pct, 1)} % (n ${g.executable.n})` : "NOT AVAILABLE"}</td>
                  <td>{num(g.mean_max_drawdown_pct, 1)}</td><td>{num(g.fast_dump_rate, 2)}</td></tr>))}</tbody></table>
            ) : null}
            {"ml_missed_winners" in d && <p className="small">Missed winners: ML {d.ml_missed_winners}, rules {d.rules_missed_winners}.
              Bad entries: ML {d.ml_bad_entries}, rules {d.rules_bad_entries}.</p>}
            {d.ml && d.rules_final && <p className="small">Bad exits: ML {d.ml.bad_exits} of {d.ml.sell} SELL, rules {d.rules_final.bad_exits} of {d.rules_final.sell}.
              Missed exits: ML {d.ml.missed_exits} of {d.ml.hold} HOLD, rules {d.rules_final.missed_exits} of {d.rules_final.hold}.</p>}
            {d.all && <p className="small">ML BUY group: {d.ml_buy.n} of {d.all.n}, mean executable return
              {" "}{num(d.ml_buy.mean_executable_return_pct, 1)} % vs {num(d.all.mean_executable_return_pct, 1)} % for all.</p>}
            {d.definitions && <p className="muted small">{Object.entries(d.definitions).map(([k, v]) => `${k}: ${v}`).join("; ")}</p>}
          </div>
        );
      })}

      <div className="section-title" style={{ marginTop: 12 }}>Frozen validation sets</div>
      {data.frozen_sets.length === 0 ? <Empty>No set frozen yet: the ml service freezes one day per family every week.</Empty> : (
        <div className="table-scroll"><table className="data-table">
          <thead><tr><th>Family</th><th>Window</th><th>Frozen</th><th>Samples at freeze</th><th>Reports</th><th>PASS</th></tr></thead>
          <tbody>{data.frozen_sets.map((s: J) => (
            <tr key={s.id}><td>{s.family}</td><td>{formatDate(s.window_start)}</td><td>{formatDate(s.frozen_at)}</td>
              <td>{s.samples_at_freeze}{s.note ? <div className="muted small">{s.note}</div> : null}</td>
              <td>{s.reports}</td><td>{s.passed}</td></tr>))}</tbody>
        </table></div>
      )}
      <p className="muted small">Not validated (no model exists): {Object.entries(data.not_validated).map(([k, v]) => `${k}: ${v}`).join("; ")}.</p>
    </Section>
  );
}
