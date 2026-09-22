"use client";

import { useState } from "react";
import Pagination from "@/components/Pagination";
import { usePagedList } from "@/lib/usePagedList";
import { formatDate } from "@/lib/format";
import type { SystemEventOut } from "@/lib/types";

const SERVICES = [
  "data-solana",
  "data-binance",
  "engine-solana-discovery",
  "engine-solana-migration",
  "engine-solana-momentum",
  "engine-binance-futures",
  "decision-engine",
  "ml",
  "paper-trading",
  "api",
];

function severityPillClass(severity: string): string {
  if (severity === "error") return "pill pill-danger";
  if (severity === "warning") return "pill pill-off";
  return "pill pill-ok";
}

export default function SystemEventsPage() {
  const [service, setService] = useState("");
  const [severity, setSeverity] = useState("");
  const { data, error, offset, setOffset, limit } = usePagedList<SystemEventOut>("/api/system/events", { service, severity });

  return (
    <div>
      <div className="page-header">
        <div className="page-title">System Events</div>
      </div>

      <div className="filters">
        <select value={service} onChange={(e) => setService(e.target.value)}>
          <option value="">All services</option>
          {SERVICES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select value={severity} onChange={(e) => setSeverity(e.target.value)}>
          <option value="">All severities</option>
          <option value="info">info</option>
          <option value="warning">warning</option>
          <option value="error">error</option>
        </select>
      </div>

      {error && <div className="error">{error}</div>}

      {data && data.items.length === 0 && <div className="empty-state">No system events match these filters.</div>}

      {data && data.items.length > 0 && (
        <>
          <table className="data-table">
            <thead>
              <tr>
                <th>Service</th>
                <th>Event</th>
                <th>Severity</th>
                <th>Detail</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((e) => (
                <tr key={e.id}>
                  <td className="mono">{e.service}</td>
                  <td>{e.event_type}</td>
                  <td>
                    <span className={severityPillClass(e.severity)}>{e.severity}</span>
                  </td>
                  <td className="mono" style={{ maxWidth: 360, fontSize: 11 }}>
                    {e.detail ? JSON.stringify(e.detail) : "—"}
                  </td>
                  <td>{formatDate(e.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination total={data.total} limit={limit} offset={offset} onOffsetChange={setOffset} />
        </>
      )}
    </div>
  );
}
