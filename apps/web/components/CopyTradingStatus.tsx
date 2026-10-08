"use client";

import { useState } from "react";
import { ErrorNotice, Loading, Section } from "@/components/ui";
import { apiPost } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const n = (v: any, d = 0, unit = "") => (v === null || v === undefined ? "unknown" : `${Number(v).toFixed(d)}${unit}`);

/** Copy trading status (yonixalpha_core.operating_mode): SUSPENDED is a
 * deliberate pause to protect trading on a small server, not a failure.
 * COPY_TRADING_ENABLED=false (system profile) keeps it SUSPENDED and
 * copy-engine stopped; it is switched back on in .env only.
 * Resume shows the current resources and is refused by the server while
 * they are below the configured thresholds. */
export default function CopyTradingStatus() {
  const q = useApi<J>("/api/copy/trading-status", undefined, { refreshMs: 30000 });
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const d = q.data;

  const set = async (status: string) => {
    setBusy(true); setErr(null);
    try { await apiPost("/api/copy/trading-status", { status }); setOpen(false); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); await q.reload(); }
  };

  if (q.loading && !d) return <Loading />;
  if (!d) return <ErrorNotice error={q.error} />;
  const suspended = d.status === "SUSPENDED";
  const c = d.current ?? {};
  return (
    <Section title="Copy Trading status">
      <div className="notice" role="status">
        <div>STATUS: <b>{d.label}</b>{d.changed_at && <span className="muted small"> · changed {formatDate(d.changed_at)} by {d.changed_by}</span>}</div>
        {suspended && (
          <>
            <div className="small">Reason: {d.reason}</div>
            <div className="small">Stopped while suspended: {d.while_suspended.stopped.join(", ")}.</div>
            <div className="small">Kept: {d.while_suspended.kept.join("; ")}.</div>
          </>
        )}
        {d.status === "THROTTLED" && <div className="small">Target trades are checked less often; wallet profiles and enrichment are paused; everything stops (except protection of open copy positions) while the server is CRITICAL.</div>}
      </div>
      <ErrorNotice error={err} />
      <div className="btn-row">
        {d.enabled === false
          ? <span className="muted small">Switched off in .env (COPY_TRADING_ENABLED=false): it cannot be resumed from here.</span>
          : suspended
          ? <button className="btn btn-sm" onClick={() => { setOpen(!open); void q.reload(); }}>Resume Copy Trading</button>
          : <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => set("SUSPENDED")}>Suspend copy trading</button>}
        {d.status === "ACTIVE" && <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => set("THROTTLED")}>Throttle</button>}
      </div>
      {open && suspended && d.enabled !== false && (
        <div className="notice">
          <div><b>Current server resources</b></div>
          <div className="small">RAM: {n(c.ram_available_mb, 0, " MB")} available of {n(c.ram_total_mb, 0, " MB")} (needs {d.resume.thresholds.min_free_ram_mb} MB)</div>
          <div className="small">CPU: load {c.load ? `${c.load["1m"]} on ${c.cpus} CPU` : "unknown"}{c.cpu_busy_pct != null && `, ${c.cpu_busy_pct}% busy`} (needs at most {d.resume.thresholds.max_cpu_load_per_cpu} per CPU)</div>
          <div className="small">Swap: {n(c.swap_used_mb, 0, " MB")} used (needs at most {d.resume.thresholds.max_swap_usage_mb} MB)</div>
          <div className="small">Database: {n(c.db_latency_ms, 1, " ms")} round trip (needs at most {d.resume.thresholds.max_db_latency_ms} ms)</div>
          <div>Recommended: <b className={d.resume.recommendation === "WAIT" ? "neg" : "pos"}>{d.resume.recommendation}</b></div>
          {d.resume.blocked_by.length > 0 && <div className="small">Not safe because: {d.resume.blocked_by.join("; ")}.</div>}
          <div className="small muted">Automatic resume is off on this server: copy trading resumes only from here, and only while every threshold holds.</div>
          <div className="btn-row">
            <button className="btn btn-sm" disabled={busy || !d.resume.safe} onClick={() => set("THROTTLED")}>Resume throttled</button>
            <button className="btn btn-ghost btn-sm" disabled={busy || !d.resume.safe} onClick={() => set("ACTIVE")}>Resume fully</button>
            <button className="btn btn-ghost btn-sm" onClick={() => setOpen(false)}>Cancel</button>
          </div>
        </div>
      )}
    </Section>
  );
}
