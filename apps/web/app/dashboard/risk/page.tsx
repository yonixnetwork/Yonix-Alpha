"use client";

import { useState } from "react";
import Pagination from "@/components/Pagination";
import { usePagedList } from "@/lib/usePagedList";
import { boolPillClass, formatDate } from "@/lib/format";
import type { RiskEventOut } from "@/lib/types";

export default function RiskEventsPage() {
  const [approved, setApproved] = useState("");
  const { data, error, offset, setOffset, limit } = usePagedList<RiskEventOut>("/api/risk/events", { approved });

  return (
    <div>
      <div className="page-header">
        <div className="page-title">Risk Events</div>
      </div>

      <div className="filters">
        <select value={approved} onChange={(e) => setApproved(e.target.value)}>
          <option value="">All outcomes</option>
          <option value="true">Approved</option>
          <option value="false">Rejected</option>
        </select>
      </div>

      {error && <div className="error">{error}</div>}

      {data && data.items.length === 0 && (
        <div className="empty-state">
          No risk events match these filters. Every strategy-signal evaluation runs through the risk engine, whatever
          it decides — see the Signals page for candidates that reached one.
        </div>
      )}

      {data && data.items.length > 0 && (
        <>
          <table className="data-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Outcome</th>
                <th>Reasons</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((e) => (
                <tr key={e.id}>
                  <td className="mono">{e.symbol ?? "—"}</td>
                  <td>
                    <span className={boolPillClass(e.approved)}>{e.approved ? "approved" : "rejected"}</span>
                  </td>
                  <td style={{ maxWidth: 400 }}>
                    {e.reasons.length === 0 ? (
                      <span style={{ color: "var(--text-dim)" }}>—</span>
                    ) : (
                      <ul className="reason-list">
                        {e.reasons.map((r, i) => (
                          <li key={i}>{r}</li>
                        ))}
                      </ul>
                    )}
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
