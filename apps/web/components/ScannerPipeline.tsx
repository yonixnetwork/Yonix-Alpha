"use client";

// Memecoin scanner pipeline (GET /api/system/scanner-health): when each
// stage last actually did something. States come from recorded activity
// only: CONNECTED, STALE, DISCONNECTED, or DATA GAP when nothing was recorded.

import { ErrorNotice, Loading, Section, StatePill, fmtDuration } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

interface Stage {
  stage: string; label: string; state: string; last_at: string | null; age_seconds: number | null; detail: string;
  stale_after_seconds: number | null; disconnected_after_seconds: number | null;
}
interface ScannerHealth {
  at: string; overall: string; stages: Stage[];
  queues: { gate_wake_depth: number; rpc_failovers_logged: number };
  pumpportal_counters: Record<string, number>;
  rpc_last_hour: Record<string, number | null>;
  pumpportal_coverage: { coverage: number | null; announced: number; seen?: number };
  notes: string[];
}

export default function ScannerPipeline({ compact = false }: { compact?: boolean }) {
  const { data, error, loading } = useApi<ScannerHealth>("/api/system/scanner-health", undefined, { refreshMs: 15000 });
  const rpc = data?.rpc_last_hour ?? {};
  return (
    <Section title="Scanner pipeline" actions={data ? <StatePill state={data.overall} /> : undefined}>
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {data && (
        <>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr><th>Stage</th><th>State</th><th>Last activity</th><th>Age</th>{!compact && <th>Stale / disconnected after</th>}</tr>
              </thead>
              <tbody>
                {data.stages.map((s) => (
                  <tr key={s.stage}>
                    <td>{s.label}</td>
                    <td><StatePill state={s.state} /></td>
                    <td>{s.last_at ? formatDate(s.last_at) : <span className="muted">DATA GAP</span>}</td>
                    <td className="mono">{s.age_seconds === null ? "-" : fmtDuration(s.age_seconds)}</td>
                    {!compact && (
                      <td className="mono muted">
                        {s.stale_after_seconds === null ? "information only" :
                          `${fmtDuration(s.stale_after_seconds)} / ${fmtDuration(s.disconnected_after_seconds ?? 0)}`}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!compact && (
            <div className="metric-grid" style={{ marginTop: 8 }}>
              <div className="metric"><span className="term-label">Gate wake queue</span>
                <span className="term-value mono">{data.queues.gate_wake_depth}</span></div>
              <div className="metric"><span className="term-label">RPC calls ok (1 h)</span>
                <span className="term-value mono">{rpc.ok ?? 0}</span></div>
              <div className="metric"><span className="term-label">RPC HTTP 429 (1 h)</span>
                <span className="term-value mono">{rpc.http_429 ?? 0}{rpc.rate_limited_share ? ` (${(Number(rpc.rate_limited_share) * 100).toFixed(1)}%)` : ""}</span></div>
              <div className="metric"><span className="term-label">RPC failed / failover retries (1 h)</span>
                <span className="term-value mono">{rpc.failed ?? 0} / {rpc.failover_retry ?? 0}</span></div>
              <div className="metric"><span className="term-label">PumpPortal disconnects</span>
                <span className="term-value mono">{data.pumpportal_counters.disconnects ?? 0}</span></div>
              <div className="metric"><span className="term-label">On-chain coverage of PumpPortal launches</span>
                <span className="term-value mono">{data.pumpportal_coverage.coverage === null ? "DATA GAP" :
                  `${(data.pumpportal_coverage.coverage * 100).toFixed(0)}% of ${data.pumpportal_coverage.announced}`}</span></div>
            </div>
          )}
          {!compact && <ul className="muted small">{data.notes.map((n) => <li key={n}>{n}</li>)}</ul>}
        </>
      )}
    </Section>
  );
}
