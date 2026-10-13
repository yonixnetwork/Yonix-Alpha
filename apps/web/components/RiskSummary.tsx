"use client";

import Link from "next/link";
import { ErrorNotice, Loading } from "@/components/ui";
import { formatDate, formatDecimal } from "@/lib/format";
import { useApi } from "@/lib/useApi";

interface AccountRisk {
  account: string; mode: string; status: string; available_sol: string | null; equity_sol: string | null;
  realized_pnl_today_sol: string; realized_loss_today_sol: string; max_daily_loss_sol: string;
  remaining_daily_loss_budget_sol: string; open_positions: number; open_positions_pnl_sol: string;
  open_positions_planned_risk_sol: string; day_starts_at: string; counts_toward_daily_limit: string;
}
interface StrategyRisk {
  engine: string; strategy_mode: string; status: string; live_eligible: boolean; trade_size: string;
  effective: Record<string, string | null>; own_daily_limit_sol: string; own_realized_pnl_today_sol: string;
  own_remaining_daily_loss_budget_sol: string; blockers: { code: string; message: string }[];
  engine_values: Record<string, unknown>; settings_errors: string[] | null; last_settings_update: string | null;
  last_decision: { at: string; decision: string; blocking_codes: string[]; status: string | null } | null;
  last_applied_trade: { at: string; mode: string; size_sol: string | null; method: string | null; entry_cost_sol: string | null;
    planned_loss_at_stop_sol: string | null; token_category: string | null } | null;
}
export interface RiskSummaryOut {
  at: string; global: Record<string, string | null>; accounts: AccountRisk[]; strategies: StrategyRisk[];
  entries_blocked_today: { total: number; by_engine_and_reason: Record<string, number> }; notes: string[];
}

// The status text is computed by the backend; only its colour is chosen here.
const statusClass = (s: string) =>
  s === "WITHIN DAILY LIMIT" ? "pill pill-ok" : s.includes("REACHED") || s.includes("KILL") || s.includes("UNAVAILABLE")
    ? "pill pill-danger" : "pill pill-warn";
const tone = (v: string | null | undefined) => (v == null ? "" : Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : "");

/** Risk limits as the backend risk engine computes them right now. `compact`
 * for the dashboard; the full view (per-strategy configuration, last
 * decision and the size applied to the last trade) on the Risk pages. */
export default function RiskSummary({ compact = false }: { compact?: boolean }) {
  const { data, error } = useApi<RiskSummaryOut>("/api/control/risk-summary", undefined, {
    refreshMs: 15000, reloadOn: ["trade.created", "trade.closed"],
  });
  if (error) return <ErrorNotice error={error} />;
  if (!data) return <Loading what="Loading risk limits…" />;
  const g = data.global;
  const blocked = Object.entries(data.entries_blocked_today.by_engine_and_reason);
  return (
    <div className="term-card" style={{ marginBottom: 16 }}>
      <div className="term-card-title">
        Risk limits <span className="muted small">· computed by the risk engine {new Date(data.at).toLocaleTimeString()}</span>
        {compact && <Link className="btn btn-ghost btn-sm" style={{ marginLeft: 8 }} href="/dashboard/risk-settings">Edit</Link>}
      </div>
      <dl className="term-kv">
        <dt>Trade sizing (GLOBAL)</dt>
        <dd>{g.trade_size_mode === "FIXED_SOL"
          ? <><b>FIXED_SOL</b> {g.fixed_trade_size_sol} SOL per trade · policy {g.fixed_size_policy}</>
          : <><b>RISK_PCT</b> {Number(g.risk_per_trade_pct) * 100}% of equity at the stop · max {g.max_position_size_quote} SOL</>}</dd>
        <dt>Max planned loss per trade</dt>
        <dd>{g.max_loss_per_trade_sol && Number(g.max_loss_per_trade_sol) > 0 ? `${g.max_loss_per_trade_sol} SOL` : "not set (RISK_PCT only)"}
          <span className="muted small"> — a stop is not a guaranteed maximum: gaps, liquidity loss and failed sells can lose more</span></dd>
        <dt>Last settings update</dt><dd>{g.last_update ? formatDate(g.last_update) : "never saved (code defaults)"}</dd>
      </dl>
      <div className="table-scroll">
        <table className="data-table">
          <thead><tr><th>Account</th><th>Status</th><th>Realized today</th><th>Daily limit</th><th>Remaining</th>
            <th>Open positions PnL</th><th>Open planned risk</th><th>Available</th></tr></thead>
          <tbody>{data.accounts.map((a) => (
            <tr key={a.account}>
              <td>{a.account} <span className={a.mode === "LIVE" ? "pill pill-danger" : "pill pill-off"}>{a.mode}</span></td>
              <td><span className={statusClass(a.status)}>{a.status}</span></td>
              <td className={`mono ${tone(a.realized_pnl_today_sol)}`}>{formatDecimal(a.realized_pnl_today_sol, 4)} SOL</td>
              <td className="mono">{a.max_daily_loss_sol} SOL</td>
              <td className="mono">{formatDecimal(a.remaining_daily_loss_budget_sol, 4)} SOL</td>
              <td className={`mono ${tone(a.open_positions_pnl_sol)}`}>{formatDecimal(a.open_positions_pnl_sol, 4)} SOL
                <span className="muted small"> ({a.open_positions} open, not in realized)</span></td>
              <td className="mono">{formatDecimal(a.open_positions_planned_risk_sol, 4)} SOL</td>
              <td className="mono">{formatDecimal(a.available_sol, 4)} SOL</td>
            </tr>))}
          </tbody>
        </table>
      </div>
      <p className="muted small">Counts toward the daily limit: {data.accounts[0]?.counts_toward_daily_limit}. Resets at 00:00 UTC.
        Reaching it blocks new entries only; open positions keep their stops and exits.</p>
      <p className="small">New entries refused by risk controls today: <b>{data.entries_blocked_today.total}</b>
        {blocked.length > 0 && <span className="muted"> ({blocked.map(([k, v]) => `${k.replace("|", " ")} ${v}`).join(", ")})</span>}
        <span className="muted"> — evaluations refused before any token data was fetched, not unique tokens</span></p>
      {!compact && (
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Strategy</th><th>Status</th><th>Trade size (saved)</th><th>Loss limits</th><th>Own today</th>
              <th>Last decision</th><th>Applied to last trade</th></tr></thead>
            <tbody>{data.strategies.map((s) => (
              <tr key={s.engine}>
                <td>{s.engine} <span className="pill pill-off">{s.strategy_mode}</span>
                  {!s.live_eligible && <div><span className="pill pill-warn">NOT LIVE-ELIGIBLE</span></div>}
                  {Object.keys(s.engine_values).length > 0 &&
                    <div className="muted small" title={JSON.stringify(s.engine_values)}>own values: {Object.keys(s.engine_values).join(", ")}</div>}</td>
                <td><span className={statusClass(s.status)}>{s.status}</span>
                  {s.blockers.map((b) => <div key={b.code} className="muted small" title={b.message}>{b.code}</div>)}
                  {s.settings_errors && <div className="error small">{s.settings_errors.join("; ")}</div>}</td>
                <td>{s.trade_size}</td>
                <td className="small">per trade {s.effective.max_loss_per_trade_sol && Number(s.effective.max_loss_per_trade_sol) > 0
                  ? `${s.effective.max_loss_per_trade_sol} SOL` : `${Number(s.effective.risk_per_trade_pct) * 100}% of equity`}
                  <br />own daily {s.own_daily_limit_sol} SOL</td>
                <td className={`mono ${tone(s.own_realized_pnl_today_sol)}`}>{formatDecimal(s.own_realized_pnl_today_sol, 4)} SOL
                  <div className="muted small">left {formatDecimal(s.own_remaining_daily_loss_budget_sol, 4)}</div></td>
                <td className="small">{s.last_decision ? <>{s.last_decision.decision}
                  {s.last_decision.status && <div><span className={statusClass(s.last_decision.status)}>{s.last_decision.status}</span></div>}
                  <div className="muted">{formatDate(s.last_decision.at)}</div></> : "—"}</td>
                <td className="small">{s.last_applied_trade ? <>{s.last_applied_trade.size_sol ?? "—"} SOL
                  <span className="pill pill-off">{s.last_applied_trade.mode}</span>
                  {s.last_applied_trade.token_category && <span className="pill pill-off">{s.last_applied_trade.token_category}</span>}
                  <div className="muted" title={s.last_applied_trade.method ?? ""}>{formatDate(s.last_applied_trade.at)}</div></> : "no trade yet"}</td>
              </tr>))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
