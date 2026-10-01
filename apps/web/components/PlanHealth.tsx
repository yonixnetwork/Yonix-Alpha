"use client";

import { useState } from "react";
import { ErrorNotice, Loading, Section } from "@/components/ui";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
export const ROLES = ["DISCOVERY", "MARKET_DATA", "EXECUTION", "CONFIRMATION", "HISTORICAL_DATA", "WALLET_DATA"];
const SEV: Record<string, string> = { UPGRADE_REQUIRED: "pill pill-danger", CONFIGURATION: "pill pill-warn", INFO: "pill pill-off" };
const CHAIN: Record<string, string> = { solana: "Solana", bsc: "BSC", robinhood: "Robinhood Chain" };

/** Roles and plan of one endpoint: role checkboxes, plan text, save. Empty roles = every role. */
export function RolesPlan({ roles, plan, disabled, onSave }: {
  roles: string[]; plan: string | null; disabled?: boolean; onSave: (body: { roles: string[]; plan: string }) => void;
}) {
  const [open, setOpen] = useState(false);
  const [r, setR] = useState<string[]>(roles);
  const [p, setP] = useState(plan ?? "");
  return (
    <div className="small">
      <span className="muted">roles: </span>{roles.length ? roles.map((x) => x.replaceAll("_", " ").toLowerCase()).join(", ") : "all"}
      <span className="muted"> · plan: </span>{plan || "not stated"}{" "}
      <button className="btn btn-ghost btn-sm" onClick={() => { setOpen(!open); setR(roles); setP(plan ?? ""); }}>{open ? "Cancel" : "Roles / plan"}</button>
      {open && (
        <div style={{ marginTop: 4 }}>
          {ROLES.map((x) => (
            <label key={x} style={{ marginRight: 8 }}><input type="checkbox" checked={r.includes(x)}
              onChange={(e) => setR(e.target.checked ? [...r, x] : r.filter((y) => y !== x))} /> {x.replaceAll("_", " ").toLowerCase()}</label>))}
          <label style={{ display: "block" }}>Plan (as named by the provider)
            <input value={p} maxLength={64} placeholder="e.g. Free, Growth, Pay As You Go" onChange={(e) => setP(e.target.value)} /></label>
          <button className="btn btn-sm" disabled={disabled} onClick={() => { onSave({ roles: r, plan: p }); setOpen(false); }}>Save roles / plan</button>
          <span className="muted"> No role ticked = serves every role.</span>
        </div>
      )}
    </div>
  );
}

/** Plan health (master §53): UPGRADE REQUIRED findings, role coverage and fallbacks. */
export default function PlanHealth({ compact = false }: { compact?: boolean }) {
  const { data, error, loading } = useApi<J>("/api/rpc/plan-health", undefined, { refreshMs: 30000 });
  if (error) return <ErrorNotice error={error} />;
  if (loading && !data) return <Loading />;
  if (!data) return null;
  const findings: J[] = compact ? data.findings.filter((f: J) => f.severity !== "INFO") : data.findings;
  return (
    <Section title={`Provider plan health${data.upgrade_required ? ` — UPGRADE REQUIRED (${data.upgrade_required})` : ""}`}>
      {findings.length === 0 ? <p className="small"><span className="pill pill-ok">NO LIMIT OBSERVED</span> <span className="muted">
        no provider limitation seen on real requests so far</span></p> : (
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Status</th><th>Chain</th><th>Provider</th><th>Current plan</th><th>Required capability</th><th>Observed limitation</th><th>Impact</th><th>Recommended upgrade</th></tr></thead>
            <tbody>{findings.map((f: J, i: number) => (
              <tr key={i}>
                <td><span className={SEV[f.severity] ?? "pill pill-off"}>{f.severity.replaceAll("_", " ")}</span></td>
                <td>{CHAIN[f.chain] ?? f.chain}</td><td>{f.provider}</td><td>{f.current_plan}</td><td>{f.capability}</td>
                <td className="small">{f.observed}</td><td className="small">{f.impact}</td><td className="small">{f.recommendation}</td>
              </tr>))}</tbody>
          </table>
        </div>)}
      {!compact && (
        <>
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>Role</th>{Object.keys(data.role_coverage).map((c) => <th key={c}>{CHAIN[c] ?? c}</th>)}</tr></thead>
              <tbody>{data.roles.map((r: string) => (
                <tr key={r}><td className="small">{r.replaceAll("_", " ")}</td>
                  {Object.keys(data.role_coverage).map((c) => {
                    const holders: string[] = data.role_coverage[c][r] ?? [];
                    const fb = (data.role_fallbacks?.[c] ?? {})[r];
                    return <td key={c} className="small">{holders.length ? holders.join(", ") : <span className="muted">any endpoint</span>}
                      {fb ? <div className="neg">fell back {fb}×</div> : null}</td>;
                  })}</tr>))}</tbody>
            </table>
          </div>
          <p className="muted small">{data.note}. Solana fallbacks are summed over the services that reported them.</p>
        </>
      )}
    </Section>
  );
}
