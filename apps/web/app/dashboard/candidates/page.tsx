"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Pagination from "@/components/Pagination";
import { usePagedList } from "@/lib/usePagedList";
import { candidateStatePillClass, formatDate, formatState } from "@/lib/format";
import type { CandidateSummary } from "@/lib/types";

const STATES = ["discovered", "observing", "qualified", "entry_pending", "entered", "managing", "exit_signal", "exiting", "closed", "rejected"];
const ENGINES = ["discovery", "migration", "momentum"];

export default function CandidatesPage() {
  const router = useRouter();
  const [state, setState] = useState("");
  const [engine, setEngine] = useState("");
  const { data, error, offset, setOffset, limit } = usePagedList<CandidateSummary>("/api/candidates", { state, engine });

  return (
    <div>
      <div className="page-header">
        <div className="page-title">Candidates</div>
      </div>

      <div className="filters">
        <select value={state} onChange={(e) => setState(e.target.value)}>
          <option value="">All states</option>
          {STATES.map((s) => (
            <option key={s} value={s}>
              {formatState(s)}
            </option>
          ))}
        </select>
        <select value={engine} onChange={(e) => setEngine(e.target.value)}>
          <option value="">All engines</option>
          {ENGINES.map((e) => (
            <option key={e} value={e}>
              {e}
            </option>
          ))}
        </select>
      </div>

      {error && <div className="error">{error}</div>}

      {data && data.items.length === 0 && (
        <div className="empty-state">
          No candidates match these filters. Solana engines create candidates as they discover activity — see the
          Overview page to check whether they&apos;re running.
        </div>
      )}

      {data && data.items.length > 0 && (
        <>
          <table className="data-table">
            <thead>
              <tr>
                <th>Mint</th>
                <th>Engine</th>
                <th>State</th>
                <th>Updated</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((c) => (
                <tr key={c.id} className="clickable" onClick={() => router.push(`/dashboard/candidates/${c.id}`)}>
                  <td className="mono">{c.mint_address}</td>
                  <td>{c.engine}</td>
                  <td>
                    <span className={candidateStatePillClass(c.state)}>{formatState(c.state)}</span>
                  </td>
                  <td>{formatDate(c.state_updated_at)}</td>
                  <td>{formatDate(c.created_at)}</td>
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
