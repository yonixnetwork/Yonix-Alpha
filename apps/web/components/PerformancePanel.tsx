"use client";

import { useState } from "react";
import LineChart from "@/components/LineChart";
import { Empty, ErrorNotice, fmtDuration, Loading, Money, Pct, Stat } from "@/components/ui";
import type { Perf, PerformanceOut } from "@/lib/cc";
import { useApi } from "@/lib/useApi";

function Breakdown({ title, rows, currency }: { title: string; rows: Record<string, Perf>; currency: string }) {
  const entries = Object.entries(rows);
  if (!entries.length) return null;
  return (
    <div className="table-wrap">
      <table className="data-table">
        <caption className="table-caption">{title}</caption>
        <thead>
          <tr>
            <th>{title.replace("By ", "")}</th>
            <th>Trades</th>
            <th>Win rate</th>
            <th>Profit factor</th>
            <th>Expectancy</th>
            <th>Total PnL</th>
            <th>Max DD</th>
          </tr>
        </thead>
        <tbody>
          {entries.map(([k, p]) => (
            <tr key={k}>
              <td>{k}</td>
              <td>{p.trades}</td>
              <td>
                <Pct value={p.win_rate} />
              </td>
              <td>{p.profit_factor ? Number(p.profit_factor).toFixed(2) : "—"}</td>
              <td>
                <Money value={p.expectancy} currency={currency} />
              </td>
              <td>
                <Money value={p.total_pnl} currency={currency} />
              </td>
              <td>
                <Money value={p.max_drawdown} currency={currency} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Paper performance (spec §48) with filters. Every figure is per account
 * currency; open positions are excluded until they close. */
export default function PerformancePanel({ account: fixedAccount, strategy: fixedStrategy, engine, hideEmpty = false }: {
  account?: string;
  strategy?: string;
  engine?: string;
  /** Hide accounts with no trades at all (e.g. a strategy's other venues). */
  hideEmpty?: boolean;
}) {
  const [account, setAccount] = useState(fixedAccount ?? "");
  const [venue, setVenue] = useState("");
  const [since, setSince] = useState("");
  const { data, error, loading } = useApi<PerformanceOut>(
    "/api/analytics/performance",
    { account: fixedAccount ?? (account || undefined), strategy: fixedStrategy, engine, venue: venue || undefined,
      since: since ? new Date(since).toISOString() : undefined },
    { reloadOn: ["trade.closed", "balance.updated"], refreshMs: 60000 },
  );
  return (
    <div>
      <div className="filters" role="group" aria-label="Performance filters">
        {!fixedAccount && (
          <select value={account} onChange={(e) => setAccount(e.target.value)} aria-label="Account">
            <option value="">All accounts</option>
            <option value="solana">Solana (SOL)</option>
            <option value="copy_solana">Solana copy trading (SOL)</option>
            <option value="evm_bsc">BSC (BNB)</option>
            <option value="evm_copy_bsc">BSC copy trading (BNB)</option>
            <option value="evm_robinhood">Robinhood Chain (ETH)</option>
            <option value="evm_copy_robinhood">Robinhood Chain copy trading (ETH)</option>
          </select>
        )}
        <select value={venue} onChange={(e) => setVenue(e.target.value)} aria-label="Venue">
          <option value="">All venues</option>
          <option value="solana">Solana</option>
        </select>
        <label className="inline-label">
          Since <input type="date" value={since} onChange={(e) => setSince(e.target.value)} />
        </label>
      </div>
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {hideEmpty && data && data.accounts.every((a) => a.overall.trades === 0) && <Empty>No closed trades yet.</Empty>}
      {data?.accounts
        .filter((a) => !hideEmpty || a.overall.trades > 0)
        .map((a) => {
        const o = a.overall;
        return (
          <div key={a.account} className="card perf-card">
            <div className="status-label">
              {a.account} · {a.currency} · since {new Date(a.reset_at).toLocaleDateString()} ·{" "}
              {a.open_positions_not_counted} open position(s) not counted
            </div>
            {o.trades === 0 ? (
              <Empty>No closed trades match these filters.</Empty>
            ) : (
              <>
                <div className="stat-grid">
                  <Stat label="Closed trades">{o.trades}</Stat>
                  <Stat label="Win / loss / BE">
                    {o.wins} / {o.losses} / {o.breakeven}
                  </Stat>
                  <Stat label="Win rate">
                    <Pct value={o.win_rate} />
                  </Stat>
                  <Stat label="Profit factor" hint={o.profit_factor ? undefined : "undefined without a losing trade"}>
                    {o.profit_factor ? Number(o.profit_factor).toFixed(2) : "—"}
                  </Stat>
                  <Stat label="Expectancy / trade">
                    <Money value={o.expectancy} currency={a.currency} />
                  </Stat>
                  <Stat label="Total PnL">
                    <Money value={o.total_pnl} currency={a.currency} />
                  </Stat>
                  <Stat label="Avg win / loss">
                    <Money value={o.avg_win} /> / <Money value={o.avg_loss} />
                  </Stat>
                  <Stat label="Largest win / loss">
                    <Money value={o.largest_win} /> / <Money value={o.largest_loss} />
                  </Stat>
                  <Stat label="Max drawdown">
                    <Money value={o.max_drawdown} currency={a.currency} /> <Pct value={o.max_drawdown_pct} />
                  </Stat>
                  <Stat label="Fees">
                    <Money value={o.fees} currency={a.currency} />
                  </Stat>
                  <Stat label="Avg holding time">{fmtDuration(o.avg_duration_seconds)}</Stat>
                </div>
                <LineChart
                  label={`Cumulative realized PnL (${a.currency})`}
                  points={(o.equity_curve ?? []).map((p) => ({ x: p.at, y: Number(p.cumulative_pnl) }))}
                />
                <Breakdown title="By strategy" rows={a.by_strategy} currency={a.currency} />
                <Breakdown title="By venue" rows={a.by_venue} currency={a.currency} />
              </>
            )}
          </div>
        );
      })}
      {data && (
        <ul className="reason-list notes">
          {data.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
