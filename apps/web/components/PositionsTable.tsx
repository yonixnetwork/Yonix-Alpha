"use client";

import Link from "next/link";
import { useState } from "react";
import { DoorOpen, Pause, Play } from "lucide-react";
import ConfirmButton from "@/components/ConfirmDialog";
import Pagination from "@/components/Pagination";
import { PnlOutcome } from "@/components/Pnl";
import { Empty, ErrorNotice } from "@/components/ui";
import { apiPost } from "@/lib/api";
import { formatDate, formatDecimal, formatPct } from "@/lib/format";
import { usePagedList } from "@/lib/usePagedList";
import { useEvents } from "@/lib/events";
import type { PaperPositionOut } from "@/lib/types";

/** Quote coin of a position's engine (evm_bsc -> BNB, evm_robinhood -> ETH, solana_* -> SOL). */
function currencyOf(engine: string | null | undefined): string | undefined {
  if (!engine) return undefined;
  if (engine.startsWith("evm_")) return engine.endsWith("bsc") ? "BNB" : engine.endsWith("robinhood") ? "ETH" : undefined;
  return engine.startsWith("solana") ? "SOL" : undefined;
}

/** Paper positions with operator controls (exit now, pause/resume
 * management). Every control asks for confirmation; the stop loss keeps
 * working while management is paused. Reloads on realtime trade events. */
export default function PositionsTable({ engine, account, strategy, status: initial = "open", title }: {
  engine?: string;
  account?: string;
  strategy?: string;
  status?: string;
  title?: string;
}) {
  const [status, setStatus] = useState(initial);
  const { data, error, offset, setOffset, limit, reload } = usePagedList<PaperPositionOut>("/api/paper/positions", {
    status: status || undefined,
    engine,
    account,
    strategy,
  });
  const [actionError, setActionError] = useState<string | null>(null);
  useEvents(["trade.created", "trade.updated", "trade.closed", "position.updated"], () => reload());

  async function act(id: string, action: "exit" | "pause" | "resume") {
    setActionError(null);
    await apiPost(`/api/paper/positions/${id}/${action}`);
    await reload();
  }

  return (
    <div>
      <div className="filters" role="group" aria-label={title ?? "Positions filter"}>
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Position status">
          <option value="open">Open</option>
          <option value="closed">Closed</option>
          <option value="">All</option>
        </select>
      </div>
      <ErrorNotice error={error || actionError} />
      {data && data.items.length === 0 && <Empty>No {status || ""} paper positions.</Empty>}
      {data && data.items.length > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Side</th>
                <th>Engine</th>
                <th>Entry</th>
                <th>Last / exit</th>
                <th>Stop</th>
                <th>Remaining</th>
                <th>PnL</th>
                <th>Opened</th>
                <th>Controls</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((p) => (
                <tr key={p.id}>
                  <td>
                    <Link href={`/dashboard/trades/${p.id}`} className="link">
                      {p.symbol}
                    </Link>
                    {p.management_paused && <span className="pill pill-warn">paused</span>}
                    {p.exit_requested && p.status === "open" && <span className="pill pill-warn">exit pending</span>}
                  </td>
                  <td>
                    <span className={p.side === "SHORT" ? "pill pill-danger" : "pill pill-ok"}>{p.side}</span>
                  </td>
                  <td className="muted">{p.engine ?? "—"}</td>
                  <td>{formatDecimal(p.entry_price, 8)}</td>
                  <td>{formatDecimal(p.status === "open" ? p.last_price ?? null : p.exit_price, 8)}</td>
                  <td>{formatDecimal(p.stop_loss, 8)}</td>
                  <td>{p.initial_quantity ? formatPct(Number(p.remaining_quantity ?? 0) / Number(p.initial_quantity), 0) : "—"}</td>
                  <td>
                    <PnlOutcome pnl={p.pnl} currency={currencyOf(p.engine)} />
                    {p.status === "closed" && p.exit_reason && <div className="form-hint">{p.exit_reason}</div>}
                  </td>
                  <td className="muted">{formatDate(p.entry_at)}</td>
                  <td>
                    {p.status === "open" ? (
                      <div className="btn-row">
                        {p.management_paused ? (
                          <ConfirmButton
                            label={<Play size={14} aria-hidden />}
                            ariaLabel={`Resume management of ${p.symbol}`}
                            title={`Resume ${p.symbol}?`}
                            body="Take-profits, trailing stop and exit intelligence resume on the next tick."
                            onConfirm={() => act(p.id, "resume")}
                          />
                        ) : (
                          <ConfirmButton
                            label={<Pause size={14} aria-hidden />}
                            ariaLabel={`Pause management of ${p.symbol}`}
                            title={`Pause ${p.symbol}?`}
                            body="Take-profits, trailing stop and exit intelligence stop acting. The stop loss is still enforced."
                            onConfirm={() => act(p.id, "pause")}
                          />
                        )}
                        <ConfirmButton
                          label={<DoorOpen size={14} aria-hidden />}
                          ariaLabel={`Exit ${p.symbol} now`}
                          title={`Exit ${p.symbol} now?`}
                          body="Paper-trading closes the remaining size against the live book/curve on its next tick. Slippage and fees apply."
                          danger
                          disabled={p.exit_requested}
                          confirmLabel="Exit now"
                          onConfirm={() => act(p.id, "exit")}
                        />
                      </div>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
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
