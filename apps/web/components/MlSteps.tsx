"use client";

import { ErrorNotice, Section } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const LABEL: Record<string, string> = {
  solana_training: "Solana model training", gate_models: "Safety-gate models", solana_shadow: "Solana shadow models",
  ablation: "Feature ablation", frozen_validation: "Frozen-set validation", evm_wallet_ml: "EVM / wallet ML",
};
const CLASS: Record<string, string> = { OK: "pill pill-ok", RUNNING: "pill pill-warn", FAILED: "pill pill-danger", INTERRUPTED: "pill pill-danger", STOPPED: "pill pill-off" };
const dur = (s: number | null | undefined) => (s === null || s === undefined ? "—" : s < 120 ? `${s} s` : s < 7200 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);

/** ml service steps: when each last ran, how long it took, failures. */
export default function MlSteps() {
  const { data, error } = useApi<J>("/api/ml/steps", undefined, { refreshMs: 30000 });
  if (error) return <ErrorNotice error={error} />;
  if (!data) return null;
  const rows = Object.keys(LABEL).map((k) => [k, data.steps[k]] as [string, J | undefined]);
  return (
    <Section title="ML service steps">
      <p className="muted small">{data.note}</p>
      <div className="table-scroll"><table className="data-table">
        <thead><tr><th>Step</th><th>State</th><th>Started</th><th>Took</th><th>Peak memory</th><th>Last success</th><th>Error</th></tr></thead>
        <tbody>{rows.map(([k, r]) => (
          <tr key={k}>
            <td>{LABEL[k]}</td>
            <td>{r ? <span className={CLASS[r.state] ?? "pill pill-off"}>{r.state}</span> : <span className="muted small">not run yet</span>}
              {r?.state === "RUNNING" && r.running_s > (data.intervals_s[k] ?? 3600) * 2
                ? <span className="pill pill-danger"> SLOW: {dur(r.running_s)}</span> : null}</td>
            <td>{r?.started_at ? formatDate(r.started_at) : "—"}</td>
            <td>{r?.state === "RUNNING" ? `${dur(r.running_s)} so far` : dur(r?.seconds)}</td>
            <td>{r?.peak_rss_mb ? `${Math.round(r.peak_rss_mb)} MB` : "—"}</td>
            <td>{r?.last_ok_at ? formatDate(r.last_ok_at) : "—"}</td>
            <td className="small muted">{r?.error ?? "—"}</td>
          </tr>))}</tbody>
      </table></div>
    </Section>
  );
}
