"use client";

import Link from "next/link";
import { useState } from "react";
import Pagination from "@/components/Pagination";
import { Empty, ErrorNotice } from "@/components/ui";
import { formatDate, formatDecimal, gateDecisionPillClass } from "@/lib/format";
import { useEvents } from "@/lib/events";
import { usePagedList } from "@/lib/usePagedList";
import type { AssessmentSummary } from "@/lib/types";

const DECISIONS = ["", "EXECUTE", "REDUCE_SIZE", "REQUIRE_MANUAL_APPROVAL", "WAIT", "REJECT", "NO_TRADE"];

/** Safety-gate decisions for one engine (or all), newest first, live. */
export default function DecisionsTable({ engine, strategy, limit = 25 }: { engine?: string; strategy?: string; limit?: number }) {
  const [decision, setDecision] = useState("");
  const { data, error, offset, setOffset, reload } = usePagedList<AssessmentSummary>(
    "/api/control/assessments",
    { engine, strategy, decision: decision || undefined },
    limit,
  );
  useEvents(["risk.updated"], (e) => {
    if ((!engine || e.data.engine === engine) && (!strategy || e.data.strategy === undefined || e.data.strategy === strategy)) reload();
  });
  return (
    <div>
      <div className="filters">
        <select value={decision} onChange={(e) => setDecision(e.target.value)} aria-label="Decision filter">
          {DECISIONS.map((d) => (
            <option key={d} value={d}>
              {d || "All decisions"}
            </option>
          ))}
        </select>
      </div>
      <ErrorNotice error={error} />
      {data && data.items.length === 0 && <Empty>No decisions yet.</Empty>}
      {data && data.items.length > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Asset</th>
                <th>Strategy</th>
                <th>Decision</th>
                <th>Risk</th>
                <th>Size</th>
                <th>Why</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((a) => (
                <tr key={a.id}>
                  <td className="muted">{formatDate(a.evaluated_at)}</td>
                  <td>
                    <Link className="link" href={`/dashboard/decisions/${a.id}`}>
                      {a.symbol ?? a.asset_id.slice(0, 8)}
                    </Link>
                  </td>
                  <td className="muted">{a.strategy}</td>
                  <td>
                    <span className={gateDecisionPillClass(a.decision)}>{a.decision}</span>
                    {a.approval_state !== "NONE" && <div className="form-hint">{a.approval_state}</div>}
                  </td>
                  <td className={`level-${a.overall_risk}`}>{a.overall_risk}</td>
                  <td>{formatDecimal(a.position_size, 6)}</td>
                  <td className="muted small">{a.reasons.slice(0, 2).join("; ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data && <Pagination total={data.total} limit={limit} offset={offset} onOffsetChange={setOffset} />}
    </div>
  );
}
