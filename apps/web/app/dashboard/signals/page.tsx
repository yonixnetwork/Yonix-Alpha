"use client";

import { useState } from "react";
import Pagination from "@/components/Pagination";
import { usePagedList } from "@/lib/usePagedList";
import { decisionPillClass, formatDate, formatDecimal } from "@/lib/format";
import type { StrategySignalOut } from "@/lib/types";

const DECISIONS = ["LONG", "SHORT", "WAIT", "NO_TRADE"];

export default function SignalsPage() {
  const [decision, setDecision] = useState("");
  const { data, error, offset, setOffset, limit } = usePagedList<StrategySignalOut>("/api/signals", { decision });

  return (
    <div>
      <div className="page-header">
        <div className="page-title">Strategy Signals</div>
      </div>

      <div className="filters">
        <select value={decision} onChange={(e) => setDecision(e.target.value)}>
          <option value="">All decisions</option>
          {DECISIONS.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
        </select>
      </div>

      {error && <div className="error">{error}</div>}

      {data && data.items.length === 0 && (
        <div className="empty-state">
          No signals match these filters. decision-engine persists one every time it evaluates a Solana candidate.
        </div>
      )}

      {data && data.items.length > 0 && (
        <>
          <table className="data-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Decision</th>
                <th>Confidence</th>
                <th>Risk Score</th>
                <th>Data Quality</th>
                <th>Reason</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((s) => (
                <tr key={s.id}>
                  <td className="mono">{s.symbol}</td>
                  <td>
                    <span className={decisionPillClass(s.decision)}>{s.decision}</span>
                  </td>
                  <td>{formatDecimal(s.confidence, 4)}</td>
                  <td>{formatDecimal(s.risk_score, 4)}</td>
                  <td>{s.data_quality}</td>
                  <td style={{ maxWidth: 320, fontSize: 12, color: "var(--text-dim)" }}>
                    {s.reason[0]}
                    {s.reason.length > 1 ? ` (+${s.reason.length - 1} more)` : ""}
                  </td>
                  <td>{formatDate(s.created_at)}</td>
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
