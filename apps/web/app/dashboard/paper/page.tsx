"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import Pagination from "@/components/Pagination";
import { apiGet, apiPost, ApiError } from "@/lib/api";
import { usePagedList } from "@/lib/usePagedList";
import { formatDate, formatDecimal, formatPct } from "@/lib/format";
import type { PaperAccountOut, PaperPositionOut } from "@/lib/types";

function AccountCard({ a, onReset }: { a: PaperAccountOut; onReset: (name: string, balance: string) => Promise<void> }) {
  const [balance, setBalance] = useState(a.starting_balance.replace(/\.?0+$/, ""));
  const winRate = a.closed_positions ? `${((a.winning_positions / a.closed_positions) * 100).toFixed(0)}%` : "—";
  return (
    <div className="card">
      <div className="status-label">
        Paper account · {a.name} ({a.quote_currency})
      </div>
      <dl className="kv">
        <dt>Equity</dt>
        <dd>{formatDecimal(a.equity, 6)}</dd>
        <dt>Cash</dt>
        <dd>{formatDecimal(a.cash_balance, 6)}</dd>
        <dt>Open positions</dt>
        <dd>
          {a.open_positions} ({formatDecimal(a.open_exposure, 6)} marked)
        </dd>
        <dt>Closed since reset</dt>
        <dd>
          {a.closed_positions} · wins {winRate}
        </dd>
        <dt>Realized PnL</dt>
        <dd className={Number(a.realized_pnl) < 0 ? "level-CRITICAL" : ""}>{formatDecimal(a.realized_pnl, 6)}</dd>
        <dt>Fees paid</dt>
        <dd>{formatDecimal(a.fees_paid, 6)}</dd>
        <dt>Since</dt>
        <dd>{formatDate(a.reset_at)}</dd>
      </dl>
      <div className="inline-form" style={{ marginTop: 10, marginBottom: 0 }}>
        <input style={{ width: 110 }} value={balance} onChange={(e) => setBalance(e.target.value)} aria-label="Starting balance" />
        <button className="btn btn-ghost btn-sm" disabled={a.open_positions > 0} onClick={() => onReset(a.name, balance)}>
          Reset
        </button>
      </div>
      {a.open_positions > 0 && <div className="form-hint">Reset is available only with no open positions.</div>}
    </div>
  );
}

export default function PaperTradingPage() {
  const [status, setStatus] = useState("");
  const [accounts, setAccounts] = useState<PaperAccountOut[]>([]);
  const [acctError, setAcctError] = useState<string | null>(null);
  const { data, error, offset, setOffset, limit } = usePagedList<PaperPositionOut>("/api/paper/positions", { status });

  const loadAccounts = useCallback(async () => {
    try {
      setAccounts(await apiGet<PaperAccountOut[]>("/api/control/paper/accounts"));
    } catch {
      setAcctError("Failed to load paper accounts.");
    }
  }, []);

  useEffect(() => {
    loadAccounts();
  }, [loadAccounts]);

  async function reset(name: string, balance: string) {
    setAcctError(null);
    try {
      await apiPost(`/api/control/paper/accounts/${name}/reset`, { starting_balance: balance });
      await loadAccounts();
    } catch (err) {
      setAcctError(err instanceof ApiError ? err.message : "Reset failed.");
    }
  }

  return (
    <div>
      <div className="page-header">
        <div className="page-title">Paper Trading</div>
      </div>
      <p className="muted">
        Simulated fills of the safety gate&apos;s own plans against the same liquidity it measured, including fees, price
        impact, partial take-profits and a trailing stop. Results are simulations, not realized profits.
      </p>

      {acctError && <div className="error">{acctError}</div>}
      <div className="card-grid">
        {accounts.map((a) => (
          <AccountCard key={a.name} a={a} onReset={reset} />
        ))}
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
          No paper positions yet. One opens when the safety gate returns EXECUTE or REDUCE_SIZE for a candidate; see
          Decisions for why candidates have not qualified.
        </div>
      )}

      {data && data.items.length > 0 && (
        <>
          <table className="data-table">
            <thead>
              <tr>
                <th>Token</th>
                <th>Engine</th>
                <th>Size</th>
                <th>Entry fill</th>
                <th>Stop / trail</th>
                <th>Last</th>
                <th>TPs hit</th>
                <th>Status</th>
                <th>PnL</th>
                <th>Opened</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((p) => {
                const remainingPct =
                  p.initial_quantity && p.remaining_quantity
                    ? formatPct(String(Number(p.remaining_quantity) / Number(p.initial_quantity)), 0)
                    : null;
                return (
                  <tr key={p.id}>
                    <td>
                      <div className="mono">{p.symbol}</div>
                      {p.assessment_id && (
                        <Link className="form-hint" href={`/dashboard/decisions/${p.assessment_id}`}>
                          decision →
                        </Link>
                      )}
                    </td>
                    <td>{p.engine ?? p.provider}</td>
                    <td>
                      {p.entry_cost_quote ? formatDecimal(p.entry_cost_quote, 4) : "—"}
                      {remainingPct && <div className="form-hint">{remainingPct} remaining</div>}
                    </td>
                    <td>{formatDecimal(p.entry_price, 12)}</td>
                    <td>
                      {p.stop_loss ? formatDecimal(p.stop_loss, 12) : "—"}
                      {p.trailing_stop && <div className="form-hint">trail {formatDecimal(p.trailing_stop, 12)}</div>}
                    </td>
                    <td>
                      {p.last_price ? formatDecimal(p.last_price, 12) : "—"}
                      {p.highest_price && (
                        <div className="form-hint">
                          hi {formatDecimal(p.highest_price, 10)} / lo {formatDecimal(p.lowest_price ?? null, 10)}
                        </div>
                      )}
                    </td>
                    <td>{p.tp_hits && p.tp_hits.length ? p.tp_hits.map((i) => `TP${i + 1}`).join(", ") : "—"}</td>
                    <td>
                      <span className={p.status === "open" ? "pill pill-ok" : "pill pill-off"}>{p.status}</span>
                      {p.exit_reason && <div className="form-hint">{p.exit_reason}</div>}
                    </td>
                    <td className={p.realized_pnl && Number(p.realized_pnl) < 0 ? "level-CRITICAL" : ""}>
                      {p.realized_pnl ? formatDecimal(p.realized_pnl, 6) : "—"}
                      {p.realized_pnl_pct && <div className="form-hint">{formatPct(p.realized_pnl_pct)}</div>}
                      {p.fees_paid_quote && <div className="form-hint">fees {formatDecimal(p.fees_paid_quote, 6)}</div>}
                    </td>
                    <td>{formatDate(p.entry_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <Pagination total={data.total} limit={limit} offset={offset} onOffsetChange={setOffset} />
        </>
      )}
    </div>
  );
}
