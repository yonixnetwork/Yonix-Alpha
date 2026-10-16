"use client";

// Alerts and the audit trail: every risk intervention, rejected or failed
// order, missing protection, stale data, connection failure, backtest
// completion or failure and strategy decision recorded by the engine, plus
// every configuration and mode change with who made it.

import { useState } from "react";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type Alert = { id: number; at: string; level: string; kind: string; source: string; message: string };
type Audit = { at: string; actor: string | null; scope: string; before: unknown; after: unknown };

export default function Alerts() {
  const [level, setLevel] = useState("");
  const [kind, setKind] = useState("");
  const [tab, setTab] = useState<"alerts" | "audit">("alerts");
  const alerts = useApi<Alert[]>("/api/crypto/alerts", { level: level || undefined, kind: kind || undefined, limit: 200 }, { refreshMs: 15000 });
  const audit = useApi<Audit[]>(tab === "audit" ? "/api/crypto/audit" : null, { limit: 200 });
  return (
    <div>
      <div className="page-header"><div className="page-title">Crypto Trading · Alerts and audit</div></div>
      <div className="btn-row" role="tablist">
        <button role="tab" aria-selected={tab === "alerts"} className={tab === "alerts" ? "btn btn-sm" : "btn btn-sm btn-ghost"} onClick={() => setTab("alerts")}>Alerts</button>
        <button role="tab" aria-selected={tab === "audit"} className={tab === "audit" ? "btn btn-sm" : "btn btn-sm btn-ghost"} onClick={() => setTab("audit")}>Configuration audit</button>
      </div>
      {tab === "alerts" && (
        <>
          <div className="strategy-filters">
            <select aria-label="Level" value={level} onChange={(e) => setLevel(e.target.value)}>
              <option value="">All levels</option><option>ERROR</option><option>WARN</option><option>INFO</option></select>
            <select aria-label="Kind" value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="">All kinds</option>
              {["risk", "execution", "connection", "backtest", "data", "strategy", "engine"].map((k) => <option key={k}>{k}</option>)}</select>
          </div>
          {alerts.error && <div className="error">{alerts.error}</div>}
          {alerts.data && alerts.data.length === 0 && <div className="empty-state">No alerts.</div>}
          {alerts.data && alerts.data.length > 0 && (
            <div className="table-scroll"><table className="data-table"><thead><tr><th>Time</th><th>Level</th><th>Kind</th><th>Source</th><th>Message</th></tr></thead>
              <tbody>{alerts.data.map((a) => (
                <tr key={a.id}><td className="small">{formatDate(a.at)}</td>
                  <td><span className={`pill ${a.level === "ERROR" ? "pill-danger" : a.level === "WARN" ? "pill-warn" : "pill-off"}`}>{a.level}</span></td>
                  <td className="small">{a.kind}</td><td className="small">{a.source}</td><td className="small">{a.message}</td></tr>))}</tbody></table></div>
          )}
        </>
      )}
      {tab === "audit" && audit.data && (
        <div className="table-scroll" style={{ marginTop: 12 }}><table className="data-table"><thead><tr><th>Time</th><th>Who</th><th>Scope</th><th>Change</th></tr></thead>
          <tbody>{audit.data.map((a, i) => (
            <tr key={i}><td className="small">{formatDate(a.at)}</td><td className="small">{a.actor ?? "—"}</td><td className="small">{a.scope}</td>
              <td><details><summary className="small">before / after</summary>
                <pre className="small mono" style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify({ before: a.before, after: a.after }, null, 2)}</pre></details></td></tr>))}</tbody></table></div>
      )}
    </div>
  );
}
