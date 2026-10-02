"use client";

import { useState } from "react";
import { ErrorNotice, Loading, Section } from "@/components/ui";
import { apiPost } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;

const CLASS_PILL: Record<string, string> = {
  ACTION_REQUIRED: "pill pill-danger", SECURITY_UPDATE: "pill pill-danger", BREAKING_CHANGE: "pill pill-warn",
  PROVIDER_CHANGE: "pill pill-warn", UPGRADE_AVAILABLE: "pill pill-ok", INFO: "pill pill-off",
};
const STATUS_PILL: Record<string, string> = { CHECKED: "pill pill-ok", ERROR: "pill pill-danger", NOT_CHECKED: "pill pill-off" };
const label = (s: string | null | undefined) => (s ? s.replaceAll("_", " ") : "");
const short = (sha: string | null | undefined) => (sha ? sha.slice(0, 10) : null);

function Ref({ w }: { w: J }) {
  if (w.kind === "pypi") {
    if (!w.installed_version && !w.latest_release) return <span className="muted">—</span>;
    return <>installed {w.installed_version ?? <span className="muted">not installed here</span>} · latest {w.latest_release ?? "—"}</>;
  }
  if (!w.latest_commit) return <span className="muted">—</span>;
  return (
    <>
      {short(w.latest_commit)}{w.latest_commit_at && <span className="muted small"> ({formatDate(w.latest_commit_at)})</span>}
      {w.latest_release && <div className="small">release {w.latest_release}</div>}
    </>
  );
}

/** GitHub / dependency update monitor (master §64-66): notify only, never deploys. */
export default function UpdateMonitor() {
  const { data, error, loading, reload } = useApi<J>("/api/system/updates", undefined, { refreshMs: 60000 });
  const [msg, setMsg] = useState<string | null>(null);
  const ack = async (id: string) => {
    try { await apiPost(`/api/system/updates/${id}/acknowledge`); setMsg(null); reload(); }
    catch (e) { setMsg(String((e as Error).message)); }
  };
  return (
    <Section title="Research / Updates">
      <ErrorNotice error={error ?? msg} />
      {loading && !data && <Loading />}
      {data && (
        <>
          <p className="muted small">
            {data.note} Checked every {Math.round(data.check_interval_s / 3600)} h · GitHub token: {data.github_token}.
            The first check of each watch is a baseline and raises no notification.
          </p>
          {Object.keys(data.unacknowledged).length > 0 && (
            <div className="notice">
              Open (not acknowledged):{" "}
              {Object.entries(data.unacknowledged as Record<string, number>).map(([k, v]) => (
                <span key={k}><span className={CLASS_PILL[k] ?? "pill pill-off"}>{label(k)}</span> {v} </span>
              ))}
            </div>
          )}
          <h4>Changes detected</h4>
          {data.events.length === 0 ? (
            <p className="muted small">No change detected since the baseline (or the monitor has not run yet).</p>
          ) : (
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr><th>Detected</th><th>Source</th><th>Class</th><th>From → to</th><th>Why it matters / reasons</th><th>Notified</th><th></th></tr></thead>
                <tbody>{(data.events as J[]).map((e) => (
                  <tr key={e.id}>
                    <td className="small">{formatDate(e.detected_at)}</td>
                    <td>{e.key.split(":")[1]}<div className="muted small">{e.summary?.category}</div></td>
                    <td><span className={CLASS_PILL[e.classification] ?? "pill pill-off"}>{label(e.classification)}</span></td>
                    <td className="small">{short(e.from_ref) ?? "—"} → {short(e.to_ref) ?? "—"}</td>
                    <td className="small">
                      {e.summary?.why}
                      {(e.summary?.reasons ?? []).length > 0 && <div>{e.summary.reasons.join("; ")}</div>}
                      {(e.summary?.messages ?? []).slice(0, 3).map((m: string, i: number) => <div key={i} className="muted">{m}</div>)}
                      {(e.summary?.vulnerabilities ?? []).length > 0 && (
                        <div className="neg">{e.summary.vulnerabilities.map((v: J) => `${v.id}${v.fixed_in?.length ? ` (fixed in ${v.fixed_in.join(", ")})` : ""}`).join(", ")}</div>
                      )}
                    </td>
                    <td className="small">{e.notified ? "yes" : "no"}</td>
                    <td>{e.acknowledged_at
                      ? <span className="muted small">acknowledged by {e.acknowledged_by}</span>
                      : <button className="btn btn-ghost btn-sm" onClick={() => ack(e.id)}>Acknowledge</button>}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
          <h4>Watched sources</h4>
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>Source</th><th>Area</th><th>Why it is watched</th><th>Check</th><th>Latest seen</th><th>Last classification</th></tr></thead>
              <tbody>{(data.watches as J[]).map((w) => (
                <tr key={w.key}>
                  <td>{w.target}<div className="muted small">{w.kind === "pypi" ? "PyPI" : "GitHub"}{w.used_directly ? " · used directly" : ""}</div></td>
                  <td className="small">{w.category}</td>
                  <td className="small">{w.why}</td>
                  <td><span className={STATUS_PILL[w.status] ?? "pill pill-off"}>{label(w.status)}</span>
                    {w.last_checked && <div className="muted small">{formatDate(w.last_checked)}</div>}
                    {w.error && <div className="neg small">{w.error}</div>}</td>
                  <td className="small"><Ref w={w} /></td>
                  <td>{w.classification
                    ? <span className={CLASS_PILL[w.classification] ?? "pill pill-off"}>{label(w.classification)}</span>
                    : <span className="muted small">{w.status === "NOT_CHECKED" ? "—" : "baseline / no change"}</span>}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </>
      )}
    </Section>
  );
}
