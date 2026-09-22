"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { apiGet, ApiError } from "@/lib/api";
import { boolPillClass, candidateStatePillClass, decisionPillClass, formatDate, formatDecimal } from "@/lib/format";
import type { CandidateDetail } from "@/lib/types";

export default function CandidateDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const [candidate, setCandidate] = useState<CandidateDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await apiGet<CandidateDetail>(`/api/candidates/${params.id}`);
      setCandidate(data);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.replace("/login");
        return;
      }
      if (err instanceof ApiError && err.status === 404) {
        setError("Candidate not found.");
        return;
      }
      setError("Failed to load candidate.");
    }
  }, [params.id, router]);

  useEffect(() => {
    load();
  }, [load]);

  if (error) return <div className="error">{error}</div>;
  if (!candidate) return <div className="empty-state">Loading...</div>;

  return (
    <div>
      <div className="page-header">
        <div>
          <Link href="/dashboard/candidates" style={{ fontSize: 13, color: "var(--text-dim)" }}>
            ← Candidates
          </Link>
          <div className="page-title mono">{candidate.mint_address}</div>
        </div>
        <span className={candidateStatePillClass(candidate.state)}>{candidate.state.replace(/_/g, " ")}</span>
      </div>

      <div className="detail-grid">
        <div className="card">
          <div className="status-label">Engine</div>
          <div className="status-value">{candidate.engine}</div>
        </div>
        <div className="card">
          <div className="status-label">State Updated</div>
          <div className="status-value" style={{ fontSize: 14 }}>
            {formatDate(candidate.state_updated_at)}
          </div>
        </div>
        <div className="card">
          <div className="status-label">Created</div>
          <div className="status-value" style={{ fontSize: 14 }}>
            {formatDate(candidate.created_at)}
          </div>
        </div>
      </div>

      <div className="page-title" style={{ fontSize: 16, marginBottom: 12 }}>
        Latest strategy signal
      </div>
      {candidate.latest_signal ? (
        <div className="card" style={{ marginBottom: 20 }}>
          <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 10 }}>
            <span className={decisionPillClass(candidate.latest_signal.decision)}>{candidate.latest_signal.decision}</span>
            <span style={{ fontSize: 13, color: "var(--text-dim)" }}>
              confidence {formatDecimal(candidate.latest_signal.confidence, 4)} · risk score{" "}
              {formatDecimal(candidate.latest_signal.risk_score, 4)} · data quality {candidate.latest_signal.data_quality}
            </span>
          </div>
          <ul className="reason-list">
            {candidate.latest_signal.reason.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="empty-state" style={{ marginBottom: 20 }}>
          No strategy signal has been recorded for this candidate yet.
        </div>
      )}

      <div className="page-title" style={{ fontSize: 16, marginBottom: 12 }}>
        Latest risk event
      </div>
      {candidate.latest_risk_event ? (
        <div className="card" style={{ marginBottom: 20 }}>
          <span className={boolPillClass(candidate.latest_risk_event.approved)}>
            {candidate.latest_risk_event.approved ? "approved" : "rejected"}
          </span>
          {candidate.latest_risk_event.reasons.length > 0 && (
            <ul className="reason-list" style={{ marginTop: 10 }}>
              {candidate.latest_risk_event.reasons.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <div className="empty-state" style={{ marginBottom: 20 }}>
          No risk evaluation has been recorded for this candidate yet.
        </div>
      )}

      <div className="page-title" style={{ fontSize: 16, marginBottom: 12 }}>
        Paper position
      </div>
      {candidate.paper_position ? (
        <div className="card" style={{ marginBottom: 20 }}>
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 13 }}>
            <span>
              <strong>{candidate.paper_position.side}</strong> @ {formatDecimal(candidate.paper_position.entry_price)}
            </span>
            <span className={candidate.paper_position.status === "open" ? "pill pill-ok" : "pill pill-off"}>
              {candidate.paper_position.status}
            </span>
            {candidate.paper_position.exit_price && (
              <span>exit @ {formatDecimal(candidate.paper_position.exit_price)} ({candidate.paper_position.exit_reason})</span>
            )}
            {candidate.paper_position.realized_pnl_pct && (
              <span>PnL {formatDecimal(candidate.paper_position.realized_pnl_pct, 4)}</span>
            )}
          </div>
        </div>
      ) : (
        <div className="empty-state" style={{ marginBottom: 20 }}>
          No paper position was opened for this candidate — see docs/PAPER_TRADING.md for why that&apos;s expected today.
        </div>
      )}

      <div className="page-title" style={{ fontSize: 16, marginBottom: 12 }}>
        State history
      </div>
      <table className="data-table" style={{ marginBottom: 20 }}>
        <thead>
          <tr>
            <th>State</th>
            <th>At</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {[...candidate.state_history].reverse().map((h, i) => (
            <tr key={i}>
              <td>
                <span className={candidateStatePillClass(h.state)}>{h.state.replace(/_/g, " ")}</span>
              </td>
              <td>{formatDate(h.at)}</td>
              <td>{h.reason ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {candidate.detail && (
        <>
          <div className="page-title" style={{ fontSize: 16, marginBottom: 12 }}>
            Engine detail
          </div>
          <pre className="card mono" style={{ overflow: "auto" }}>
            {JSON.stringify(candidate.detail, null, 2)}
          </pre>
        </>
      )}
    </div>
  );
}
