"use client";

import Link from "next/link";
import { LayoutDashboard } from "lucide-react";
import { SellButton } from "@/components/ManualTrade";
import { ErrorNotice, Loading, PageHeader, Section, StatePill, fmtDuration, modeClass } from "@/components/ui";
import { formatDecimal } from "@/lib/format";
import { useApi } from "@/lib/useApi";

interface Today { realized_pnl_sol: string; unrealized_pnl_sol: string; trades: number; closed: number; wins: number; losses: number;
  open_positions: number; avg_decision_to_confirm_ms?: number | null }
interface Pos { id: string; symbol: string; mint: string | null; mode: string; status: string; route: string | null; entry_price: string;
  last_price: string | null; pnl_sol: string; pnl_pct: string | null; age_seconds: number | null; last_marked_at: string | null }
interface Summary {
  wallet: null | { sol: string | null; at: string | null; token_holdings: number | null; available_sol: string | null; reserve_sol: string };
  today: { LIVE: Today; PAPER: Today };
  market: { fresh_last_hour: number; observing: number; migrated_last_hour: number; momentum_active: number; active_opportunities: number };
  system: { data: { stream_heartbeat_age_seconds: number | null; state: string }; execution: { state: string; reason: string | null };
    ml: { active_models: number }; connections: Record<string, string>; problems: Record<string, { state: string; detail: string | null }> };
  positions: Pos[]; global_mode: string; kill_switch: boolean; at: string;
}

const tone = (v: string | number | null | undefined) => (v === null || v === undefined ? "" : Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : "");

function Metric({ label, value, sub, className }: { label: string; value: React.ReactNode; sub?: React.ReactNode; className?: string }) {
  return (
    <div className="metric">
      <span className="term-label">{label}</span>
      <span className={`term-value mono ${className ?? ""}`}>{value}</span>
      {sub !== undefined && <span className="term-sub">{sub}</span>}
    </div>
  );
}

function TodayPanel({ mode, t }: { mode: string; t: Today }) {
  return (
    <div className="term-card">
      <div className="term-card-title">Today · {mode}</div>
      <div className="metric-grid">
        <Metric label="Realized PnL" value={`${formatDecimal(t.realized_pnl_sol, 4)} SOL`} className={tone(t.realized_pnl_sol)} />
        <Metric label="Unrealized PnL" value={`${formatDecimal(t.unrealized_pnl_sol, 4)} SOL`} className={tone(t.unrealized_pnl_sol)} />
        <Metric label="Trades" value={t.trades} sub={`${t.open_positions} open`} />
        <Metric label="Win / loss" value={<><span className="pos">{t.wins}</span> / <span className="neg">{t.losses}</span></>} sub={`${t.closed} closed`} />
        {mode === "LIVE" && <Metric label="Avg decision → confirm" value={t.avg_decision_to_confirm_ms != null ? `${t.avg_decision_to_confirm_ms} ms` : "—"}
          sub="confirmed LIVE buys today" />}
      </div>
    </div>
  );
}

/** Solana memecoin trading dashboard: live wallet, today, market, system
 * health and open positions — every value measured, updated live. */
export default function DashboardPage() {
  const { data, error } = useApi<Summary>("/api/summary/memecoin", undefined, {
    refreshMs: 10000, reloadOn: ["balance.updated", "trade.created", "trade.closed", "trade.updated"],
  });
  if (error) return <ErrorNotice error={error} />;
  if (!data) return <Loading />;
  const w = data.wallet;
  const unhealthy = Object.entries(data.system.problems ?? {});
  return (
    <div>
      <PageHeader title="Dashboard" icon={<LayoutDashboard size={20} aria-hidden />}
        subtitle={<><span className={modeClass(data.global_mode)}>{data.global_mode}</span>{data.kill_switch && <span className="pill pill-danger"> KILL SWITCH ON</span>}</>} />
      <div className="dash-grid">
        <div className="term-card">
          <div className="term-card-title">Live wallet</div>
          {!w ? <p className="muted">Wallet not synced (live execution disabled or not reporting).</p> : (
            <div className="metric-grid">
              <Metric label="SOL" value={formatDecimal(w.sol, 4)} sub={w.at ? `synced ${new Date(w.at).toLocaleTimeString()}` : undefined} />
              <Metric label="Available" value={formatDecimal(w.available_sol, 4)} sub={`${w.reserve_sol} SOL kept for fees`} />
              <Metric label="Token holdings" value={w.token_holdings ?? "—"} sub="mints with a balance" />
            </div>
          )}
        </div>
        <TodayPanel mode="LIVE" t={data.today.LIVE} />
        <TodayPanel mode="PAPER" t={data.today.PAPER} />
        <div className="term-card">
          <div className="term-card-title">Market</div>
          <div className="metric-grid">
            <Metric label="Fresh (1 h)" value={<Link href="/dashboard/solana/fresh">{data.market.fresh_last_hour}</Link>} />
            <Metric label="Observing" value={<Link href="/dashboard/solana/observing">{data.market.observing}</Link>} />
            <Metric label="Migrated (1 h)" value={<Link href="/dashboard/solana/migrated">{data.market.migrated_last_hour}</Link>} />
            <Metric label="Momentum" value={<Link href="/dashboard/solana/momentum">{data.market.momentum_active}</Link>} />
            <Metric label="Active opportunities" value={<Link href="/dashboard/funnel">{data.market.active_opportunities}</Link>} />
          </div>
        </div>
        <div className="term-card">
          <div className="term-card-title">System</div>
          <dl className="term-kv">
            <dt>Solana connections</dt><dd>{unhealthy.length === 0 ? <StatePill state="CONNECTED" />
              : <Link href="/dashboard/health" title={unhealthy.map(([k, v]) => `${k}: ${v.state}${v.detail ? ` (${v.detail})` : ""}`).join("\n")}>
                {unhealthy.map(([k, v]) => `${k} ${v.state}`).join(", ")}</Link>}</dd>
            <dt>Trade stream</dt><dd><StatePill state={data.system.data.state} /> <span className="muted">{data.system.data.stream_heartbeat_age_seconds != null ? `${data.system.data.stream_heartbeat_age_seconds}s ago` : ""}</span></dd>
            <dt>Execution worker</dt><dd><span className={data.system.execution.state === "ready" ? "pill pill-ok" : "pill pill-warn"}>{data.system.execution.state.toUpperCase()}</span></dd>
            <dt>ML</dt><dd>{data.system.ml.active_models} active model{data.system.ml.active_models === 1 ? "" : "s"}</dd>
          </dl>
          {data.system.execution.reason && <p className="muted small">{data.system.execution.reason}</p>}
        </div>
      </div>
      <Section title="Open positions" actions={<Link className="btn btn-ghost btn-sm" href="/dashboard/positions">All</Link>}>
        {data.positions.length === 0 ? <p className="muted">No open positions.</p> : (
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>Token</th><th>Mode</th><th>Entry</th><th>Current</th><th>PnL</th><th>Route</th><th>Age</th><th /></tr></thead>
              <tbody>{data.positions.map((p) => (
                <tr key={p.id}>
                  <td>{p.mint ? <Link className="link" href={`/dashboard/tokens/${p.mint}`}>{p.symbol}</Link> : p.symbol}
                    <div><Link className="muted small" href={`/dashboard/trades/${p.id}`}>trade details</Link></div></td>
                  <td><span className={p.mode === "LIVE" ? "pill pill-danger" : "pill pill-off"}>{p.mode}</span>{p.status !== "open" && <span className="pill pill-warn"> {p.status}</span>}</td>
                  <td className="mono">{formatDecimal(p.entry_price, 12)}</td>
                  <td className="mono">{formatDecimal(p.last_price, 12)}</td>
                  <td className={`mono ${tone(p.pnl_sol)}`}>{formatDecimal(p.pnl_sol, 5)} SOL {p.pnl_pct && <span>({p.pnl_pct}%)</span>}</td>
                  <td>{p.route ?? "—"}</td>
                  <td>{fmtDuration(p.age_seconds)}</td>
                  <td>{p.status === "open" && <SellButton positionId={p.id} symbol={p.symbol} mode={p.mode} route={p.route} />}</td>
                </tr>))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  );
}
