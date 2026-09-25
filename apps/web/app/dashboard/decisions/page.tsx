"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Pagination from "@/components/Pagination";
import { apiPost, ApiError } from "@/lib/api";
import { usePagedList } from "@/lib/usePagedList";
import { formatDate, formatDecimal, gateDecisionPillClass } from "@/lib/format";
import type { AssessmentSummary } from "@/lib/types";

const DECISIONS = ["EXECUTE", "REDUCE_SIZE", "WAIT", "REQUIRE_MANUAL_APPROVAL", "NO_TRADE", "REJECT"];

export default function DecisionsPage() {
  const router = useRouter();
  const [decision, setDecision] = useState("");
  const [engine, setEngine] = useState("");
  const [approval, setApproval] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const { data, error, offset, setOffset, limit, reload } = usePagedList<AssessmentSummary>("/api/control/assessments", {
    decision,
    engine,
    approval_state: approval,
  });

  async function act(id: string, action: "approve" | "decline") {
    setMessage(null);
    setActionError(null);
    try {
      await apiPost(`/api/control/assessments/${id}/${action}`);
      setMessage(
        action === "approve"
          ? "Approved. The next evaluation re-runs every safety check on fresh data; it enters only if all still pass."
          : "Declined. The candidate was rejected.",
      );
      reload();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Action failed.");
    }
  }

  return (
    <div>
      <div className="page-header">
        <div className="page-title">Decisions</div>
      </div>
      <p className="muted">
        Every safety-gate evaluation, including rejected, waiting and no-trade outcomes, with the reasons it gave.
        Approving an item never skips a check.
      </p>

      <div className="filters">
        <select value={decision} onChange={(e) => setDecision(e.target.value)}>
          <option value="">All decisions</option>
          {DECISIONS.map((d) => (
            <option key={d} value={d}>
              {d}
            </option>
          ))}
          <option value="REJECT,NO_TRADE,WAIT,REQUIRE_MANUAL_APPROVAL">Not taken</option>
        </select>
        <select value={engine} onChange={(e) => setEngine(e.target.value)}>
          <option value="">All engines</option>
          <option value="solana_fresh">solana_fresh</option>
          <option value="solana_migration">solana_migration</option>
          <option value="binance_futures">binance_futures</option>
        </select>
        <select value={approval} onChange={(e) => setApproval(e.target.value)}>
          <option value="">Any approval state</option>
          <option value="PENDING">Pending approval</option>
          <option value="APPROVED">Approved</option>
          <option value="DECLINED">Declined</option>
          <option value="EXPIRED">Expired</option>
        </select>
      </div>

      {message && <div className="success">{message}</div>}
      {actionError && <div className="error">{actionError}</div>}
      {error && <div className="error">{error}</div>}

      {data && data.items.length === 0 && (
        <div className="empty-state">
          No decisions match. Decisions appear once the pump.fun stream funnel promotes a candidate and the
          decision engine evaluates it. See Strategy Center for pipeline health.
        </div>
      )}

      {data && data.items.length > 0 && (
        <>
          <table className="data-table">
            <thead>
              <tr>
                <th>Token</th>
                <th>Engine</th>
                <th>Decision</th>
                <th>Risk</th>
                <th>Size</th>
                <th>Reasons</th>
                <th>Evaluated</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((a) => (
                <tr key={a.id} className="clickable" onClick={() => router.push(`/dashboard/decisions/${a.id}`)}>
                  <td>
                    <div>{a.symbol ?? "—"}</div>
                    <div className="mono muted">{a.asset_id.slice(0, 10)}…</div>
                  </td>
                  <td>{a.engine}</td>
                  <td>
                    <span className={gateDecisionPillClass(a.decision)}>{a.decision}</span>
                    <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
                      {a.status_label}
                      {a.approval_state !== "NONE" ? ` · ${a.approval_state}` : ""}
                    </div>
                  </td>
                  <td className={`level-${a.overall_risk}`}>{a.overall_risk}</td>
                  <td>{a.position_size ? formatDecimal(a.position_size, 4) : "—"}</td>
                  <td style={{ maxWidth: 380 }}>
                    <ul className="reason-list">
                      {a.reasons.slice(0, 3).map((r, i) => (
                        <li key={i}>{r}</li>
                      ))}
                    </ul>
                  </td>
                  <td>{formatDate(a.evaluated_at)}</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    {a.approval_state === "PENDING" && (
                      <div className="btn-row">
                        <button className="btn btn-sm" onClick={() => act(a.id, "approve")}>
                          Approve
                        </button>
                        <button className="btn btn-ghost btn-sm" onClick={() => act(a.id, "decline")}>
                          Decline
                        </button>
                      </div>
                    )}
                  </td>
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
