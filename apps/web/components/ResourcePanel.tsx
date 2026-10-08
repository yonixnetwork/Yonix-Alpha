"use client";

import { useState } from "react";
import { ErrorNotice, Loading, Section, Stat } from "@/components/ui";
import { apiPut } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const LEVEL_CLASS: Record<string, string> = { NORMAL: "pill pill-ok", WARNING: "pill pill-warn", CRITICAL: "pill pill-danger", UNKNOWN: "pill pill-off" };
const n = (v: any, d = 0, unit = "") => (v === null || v === undefined ? "—" : `${Number(v).toFixed(d)}${unit}`);

/** Server resources and the system resource mode (apps/api routes/resources,
 * low-resource operation 2026-10-08): host memory / swap / load / CPU /
 * disk and pressure, Postgres and Redis, each service's memory and CPU from
 * its heartbeat, the resource level and what is paused because of it. */
export default function ResourcePanel() {
  const q = useApi<J>("/api/system/resources", undefined, { refreshMs: 30000 });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const d = q.data;

  const setMode = async (mode: string) => {
    if (mode === d?.mode?.resource_mode) return;
    const warn = mode === "EMERGENCY" ? "EMERGENCY pauses all ML training, review refreshes and copy trading. " :
      mode === "NORMAL" ? "NORMAL runs ML training hourly again; on this server that competes with trading for CPU and memory. " : "";
    if (!window.confirm(`${warn}Set the resource mode to ${mode}?`)) return;
    setBusy(true); setErr(null);
    try { await apiPut("/api/system/resource-mode", { mode }); await q.reload(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  const m = d?.host?.memory ?? {};
  const load = d?.host?.load ?? {};
  const psi = d?.host?.pressure ?? {};
  return (
    <Section title="Server resources"
      actions={d && <span className="pill pill-warn" title={`set by ${d.mode.source}`}>{d.mode.label}</span>}>
      <ErrorNotice error={q.error || err} />
      {q.loading && !d && <Loading />}
      {d && (
        <>
          <div className="notice">
            Resource level: <span className={LEVEL_CLASS[d.level] ?? "pill"}>{d.level}</span>
            {d.level_reasons.length > 0 && <> — {d.level_reasons.join("; ")}</>}
            {d.system_profile && <div className="small">System profile: <b>{d.system_profile.label}</b> — chains running:{" "}
              {d.system_profile.enabled_chains.join(", ")}; copy trading {d.system_profile.copy_trading_enabled ? "on" : "off"};
              ML scope {d.system_profile.ml.model_scope}.</div>}
            {d.paused_now.length > 0 && <div className="small">Paused or reduced now: {d.paused_now.join(", ")}.</div>}
            <div className="small muted">Never paused: {d.priorities.never_paused.join(", ")}.</div>
          </div>
          <div className="btn-row" role="group" aria-label="Resource mode">
            {["NORMAL", "LOW_RESOURCE", "EMERGENCY"].map((x) => (
              <button key={x} disabled={busy} className={d.mode.resource_mode === x ? "btn btn-sm" : "btn btn-ghost btn-sm"}
                onClick={() => setMode(x)}>{x.replace("_", " ")}</button>))}
            {d.mode.changed_at && <span className="muted small">changed {formatDate(d.mode.changed_at)} by {d.mode.changed_by}</span>}
          </div>
          <div className="stat-grid">
            <Stat label="RAM available / total">{n(m.available_mb, 0, " MB")} / {n(m.total_mb, 0, " MB")}</Stat>
            <Stat label="RAM used">{n(m.used_mb, 0, " MB")}</Stat>
            <Stat label="Swap used">{n(m.swap_used_mb, 0, " MB")} of {n(m.swap_total_mb, 0, " MB")}</Stat>
            <Stat label={`Load (1 / 5 / 15 min), ${d.host.cpus} CPU`}>{n(load["1m"], 2)} / {n(load["5m"], 2)} / {n(load["15m"], 2)}</Stat>
            <Stat label="CPU busy" hint="between the last two readings of this page">{n(d.host.cpu_busy_pct, 1, "%")}</Stat>
            <Stat label="Swap in / out" hint="pages per second between the last two readings">
              {n(d.host.vm?.pswpin_per_s, 1)} / {n(d.host.vm?.pswpout_per_s, 1)}</Stat>
            <Stat label="Pressure (avg 60 s): CPU / memory / disk I/O" hint="share of time tasks waited (Linux PSI); — = not provided by this kernel">
              {n(psi.cpu?.some?.avg60, 1, "%")} / {n(psi.memory?.some?.avg60, 1, "%")} / {n(psi.io?.some?.avg60, 1, "%")}</Stat>
            <Stat label="Disk used">{d.host.disk ? `${d.host.disk.used_gb} of ${d.host.disk.total_gb} GB (${d.host.disk.used_pct}%)` : "—"}</Stat>
            <Stat label="Postgres connections">{Object.entries(d.postgres.connections ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "—"}</Stat>
            <Stat label="Postgres statements > 2 s">{d.postgres.statements_over_2s ?? "—"}</Stat>
            <Stat label="Database size">{n(d.postgres.database_size_mb, 0, " MB")}</Stat>
            <Stat label="Redis memory / keys">{n(d.redis.used_mb, 1, " MB")} / {d.redis.keys ?? "—"}</Stat>
          </div>
          <details>
            <summary className="small">Postgres settings in force</summary>
            <p className="small mono">{Object.entries(d.postgres.settings ?? {}).map(([k, v]) => `${k}=${v}`).join("  ")}</p>
          </details>
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>Service</th><th>Memory</th><th>CPU</th><th>Heartbeat</th></tr></thead>
              <tbody>{d.workers.map((w: J) => (
                <tr key={w.service}><td>{w.service}</td><td>{n(w.rss_mb, 0, " MB")}</td>
                  <td title="share of one CPU between its last two heartbeats">{n(w.cpu_pct, 1, "%")}</td>
                  <td className="small">{w.heartbeat ? formatDate(w.heartbeat) : w.disabled ? <span className="pill pill-off" title={w.disabled}>DISABLED</span> : "none"}</td></tr>))}</tbody>
            </table>
          </div>
          <p className="muted small">{d.note}</p>
        </>
      )}
    </Section>
  );
}
