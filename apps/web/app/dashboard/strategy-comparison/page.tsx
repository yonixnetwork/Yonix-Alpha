"use client";

// Strategy Comparison (GET /api/strategies/comparison): which strategies
// actually work, from closed positions only, PAPER and LIVE apart. The
// "without best trade" column shows whether a result depends on one trade.

import { useState } from "react";
import { GitCompare } from "lucide-react";
import { Empty, ErrorNotice, Loading, PageHeader, Section, fmtDuration } from "@/components/ui";
import { useApi } from "@/lib/useApi";

interface Row {
  key: string; mode: string; trades: number; wins: number; win_rate: string | null; net_sol: string | null;
  avg_win_sol: string | null; avg_loss_sol: string | null; profit_factor: string | null; max_drawdown_sol: string | null;
  fees_sol: string | null; avg_size_sol: string | null; median_hold_seconds: number | null; net_without_best_sol: string | null;
  best: { symbol: string | null; pnl_sol: string | null }; worst: { symbol: string | null; pnl_sol: string | null };
  sample: string;
}
interface Comparison {
  since: string | null; trades: number; min_sample: number; strategy: Row[]; category: Row[]; strategy_x_category: Row[];
  notes: string[];
}

const WINDOWS: [string, number | null][] = [["All history", null], ["Last 7 days", 7], ["Last 3 days", 3], ["Last 24 h", 1]];

function sol(v: string | null, digits = 4) {
  if (v === null || v === undefined) return <span className="muted">-</span>;
  const n = Number(v);
  return <span className={n > 0 ? "pos mono" : n < 0 ? "neg mono" : "mono"}>{n >= 0 ? "+" : ""}{n.toFixed(digits)}</span>;
}

function Table({ rows }: { rows: Row[] }) {
  if (!rows.length) return <Empty>No closed Solana position in this window.</Empty>;
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>Mode</th><th>Group</th><th>Trades</th><th>Win rate</th><th>Net SOL</th><th>Without best trade</th>
            <th>Avg win / loss</th><th>Profit factor</th><th>Max drawdown</th><th>Fees</th><th>Avg size</th>
            <th>Median hold</th><th>Best / worst token</th><th>Sample</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.mode}-${r.key}`}>
              <td><span className={r.mode === "LIVE" ? "pill pill-danger" : "pill pill-off"}>{r.mode}</span></td>
              <td>{r.key}</td>
              <td className="mono">{r.trades}</td>
              <td className="mono">{r.win_rate === null ? "-" : `${(Number(r.win_rate) * 100).toFixed(0)}%`}</td>
              <td>{sol(r.net_sol)}</td>
              <td>{sol(r.net_without_best_sol)}</td>
              <td>{sol(r.avg_win_sol, 5)} / {sol(r.avg_loss_sol, 5)}</td>
              <td className="mono">{r.profit_factor ?? "-"}</td>
              <td className="mono">{r.max_drawdown_sol === null ? "-" : Number(r.max_drawdown_sol).toFixed(4)}</td>
              <td className="mono">{r.fees_sol === null ? "-" : Number(r.fees_sol).toFixed(4)}</td>
              <td className="mono">{r.avg_size_sol === null ? "-" : Number(r.avg_size_sol).toFixed(4)}</td>
              <td className="mono">{fmtDuration(r.median_hold_seconds)}</td>
              <td className="small">{r.best.symbol ?? "-"} {sol(r.best.pnl_sol, 4)} · {r.worst.symbol ?? "-"} {sol(r.worst.pnl_sol, 4)}</td>
              <td><span className={r.sample === "ok" ? "pill pill-ok" : "pill pill-warn"}>{r.sample}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function StrategyComparisonPage() {
  const [days, setDays] = useState<number | null>(null);
  const since = days === null ? null : new Date(Date.now() - days * 86400000).toISOString().slice(0, 13) + ":00:00Z";
  const { data, error, loading } = useApi<Comparison>(
    since ? `/api/strategies/comparison?since=${encodeURIComponent(since)}` : "/api/strategies/comparison", undefined,
    { refreshMs: 60000 });
  return (
    <div>
      <PageHeader title="Strategy Comparison" icon={<GitCompare size={20} aria-hidden />}
        subtitle="Which strategies actually work: closed Solana positions only, net of every fee, PAPER and LIVE apart.">
        <div className="btn-row">
          {WINDOWS.map(([label, d]) => (
            <button key={label} className={`btn btn-sm ${days === d ? "" : "btn-ghost"}`} onClick={() => setDays(d)}>{label}</button>
          ))}
        </div>
      </PageHeader>
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {data && (
        <>
          <div className="notice">
            {data.trades} closed trades{data.since ? ` since ${data.since}` : ""}. A group is judged only with at least {data.min_sample} trades;
            a group positive only because of its best trade has no demonstrated edge.
          </div>
          <Section title="By strategy"><Table rows={data.strategy} /></Section>
          <Section title="By token category at entry"><Table rows={data.category} /></Section>
          <Section title="Strategy x category"><Table rows={data.strategy_x_category} /></Section>
          <ul className="muted small">{data.notes.map((n) => <li key={n}>{n}</li>)}</ul>
        </>
      )}
    </div>
  );
}
