"use client";

import { useState } from "react";
import Pagination from "@/components/Pagination";
import { usePagedList } from "@/lib/usePagedList";
import { formatDate, formatDecimal } from "@/lib/format";
import type { PaperPositionOut } from "@/lib/types";

export default function PaperTradingPage() {
  const [status, setStatus] = useState("");
  const { data, error, offset, setOffset, limit } = usePagedList<PaperPositionOut>("/api/paper/positions", { status });

  return (
    <div>
      <div className="page-header">
        <div className="page-title">Paper Trading</div>
      </div>

      <div className="filters">
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All positions</option>
          <option value="open">Open</option>
          <option value="closed">Closed</option>
        </select>
      </div>

      {error && <div className="error">{error}</div>}

      {data && data.items.length === 0 && (
        <div className="empty-state">
          No paper position has ever opened — see docs/PAPER_TRADING.md for the two gates that currently block every
          attempt.
        </div>
      )}

      {data && data.items.length > 0 && (
        <>
          <table className="data-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Side</th>
                <th>Provider</th>
                <th>Entry</th>
                <th>Status</th>
                <th>Exit</th>
                <th>Realized PnL</th>
                <th>Entry At</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((p) => (
                <tr key={p.id}>
                  <td className="mono">{p.symbol}</td>
                  <td>{p.side}</td>
                  <td>{p.provider}</td>
                  <td>{formatDecimal(p.entry_price)}</td>
                  <td>
                    <span className={p.status === "open" ? "pill pill-ok" : "pill pill-off"}>{p.status}</span>
                  </td>
                  <td>
                    {p.exit_price ? `${formatDecimal(p.exit_price)} (${p.exit_reason})` : "—"}
                  </td>
                  <td>{p.realized_pnl_pct ? formatDecimal(p.realized_pnl_pct, 4) : "—"}</td>
                  <td>{formatDate(p.entry_at)}</td>
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
