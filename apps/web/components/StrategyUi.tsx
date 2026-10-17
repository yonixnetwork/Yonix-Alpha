"use client";

// Shared pieces of the Crypto Trading Strategy Center. Definitions match
// yonixalpha_crypto/backtest/metrics.py; a value the backend could not
// compute is shown as "n/a" with the reason, never estimated here.

import { ApiError } from "@/lib/api";

export const DEFS: Record<string, string> = {
  trades: "Completed trades in the period (a position open at the end is closed at the last price and counted separately).",
  net_profit: "Sum of every trade's result after fees, spread, slippage and funding, in USDT.",
  net_return_pct: "Net profit divided by the starting equity.",
  win_rate: "Winning trades / all trades. Never used to rank: a high win rate can still lose money.",
  profit_factor: "Gross profit / gross loss. Above 1 means winners outweigh losers. n/a when there were no losses.",
  expectancy: "Average result per trade, in USDT.",
  expectancy_r: "Average result per trade in R: 1 R is the loss planned at entry (distance to the stop x size + costs).",
  max_drawdown_pct: "Largest fall of the equity curve from a previous high, in %.",
  sharpe: "Mean daily return / its standard deviation, annualised with sqrt(365). n/a with fewer than 30 days.",
  sortino: "Like Sharpe, but only downside volatility counts. n/a with fewer than 30 days.",
  exposure_pct: "Share of bars with an open position.",
  avg_holding_hours: "Average time a trade stayed open.",
  fees: "Exchange fees paid (assumed rates stated in the run's cost assumptions).",
  slippage: "Cost of filling worse than the reference price.",
  tier: "Documented verdict: candidate, weak, insufficient evidence or failed (see the ranking rule).",
};

export function errText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  return e instanceof Error ? e.message : String(e);
}

export function Info({ k, text }: { k?: string; text?: string }) {
  const t = text ?? (k ? DEFS[k] : "");
  return t ? <span className="info" title={t} aria-label={t} role="img">i</span> : null;
}

export const n = (v: unknown, d = 2): string => {
  if (v === null || v === undefined || v === "") return "n/a";
  const x = Number(v);
  return Number.isFinite(x) ? x.toLocaleString(undefined, { maximumFractionDigits: d }) : String(v);
};

export function TierPill({ tier }: { tier?: string | null }) {
  if (!tier) return <span className="pill pill-off">no backtest</span>;
  const cls = tier === "candidate" ? "pill-ok" : tier === "failed" ? "pill-danger" : "pill-warn";
  return <span className={`pill ${cls}`} title={DEFS.tier}>{tier}</span>;
}

export const MODE_LABEL: Record<string, string> = { DISABLED: "DISABLED", DEMO: "PAPER", EXCHANGE_DEMO: "EXCHANGE DEMO" };

export function ModePill({ mode, enabled }: { mode: string; enabled?: boolean }) {
  const cls = !enabled || mode === "DISABLED" ? "pill-off" : mode === "EXCHANGE_DEMO" ? "pill-warn" : "pill-ok";
  return <span className={`pill ${cls}`}>{MODE_LABEL[mode] ?? mode}{enabled === false && mode !== "DISABLED" ? " (off)" : ""}</span>;
}

// One pill per lifecycle status (crypto_center.lifecycle).
export function StatusPill({ status }: { status: string }) {
  const cls = ["backtest completed", "exchange demo active"].includes(status) ? "pill-ok"
    : ["backtest failed", "exchange demo unavailable", "rejected / disabled"].includes(status) ? "pill-danger"
      : ["paper trading active", "backtest running"].includes(status) ? "pill-warn" : "pill-off";
  return <span className={`pill ${cls}`}>{status}</span>;
}

type M = Record<string, unknown> & { period?: { from?: string | null; to?: string | null; bars?: number } };

const ROWS: [string, string, number][] = [
  ["trades", "Trades", 0], ["net_profit", "Net profit (USDT)", 2], ["net_return_pct", "Net return %", 2],
  ["gross_profit", "Gross profit", 2], ["gross_loss", "Gross loss", 2], ["win_rate", "Win rate", 3],
  ["avg_win", "Average win", 2], ["avg_loss", "Average loss", 2], ["expectancy", "Expectancy (USDT)", 3],
  ["expectancy_r", "Expectancy (R)", 3], ["profit_factor", "Profit factor", 2], ["max_drawdown_pct", "Max drawdown %", 2],
  ["sharpe", "Sharpe", 2], ["sortino", "Sortino", 2], ["avg_holding_hours", "Avg holding (h)", 1],
  ["fees", "Fees", 2], ["slippage", "Slippage", 2], ["funding", "Funding", 2], ["exposure_pct", "Exposure %", 1],
  ["max_consecutive_wins", "Max wins in a row", 0], ["max_consecutive_losses", "Max losses in a row", 0],
  ["partial_fills", "Partial fills", 0],
];

export function MetricsTable({ m, label }: { m: M | null | undefined; label: string }) {
  if (!m) return <div className="empty-state">{label}: no results yet.</div>;
  return (
    <div>
      <div className="small" style={{ marginBottom: 6 }}>
        <strong>{label}</strong> · {String(m.trades ?? m.closed_trades ?? 0)} trades
        {m.period?.from ? ` · ${String(m.period.from).slice(0, 10)} to ${String(m.period.to ?? "").slice(0, 10)}` : ""}
        {m.insufficient_evidence ? <span className="pill pill-warn" style={{ marginLeft: 6 }}>insufficient evidence</span> : null}
        {m.unavailable_reason ? <div className="muted">{String(m.unavailable_reason)}</div> : null}
        {m.ratio_unavailable_reason ? <div className="muted">Sharpe / Sortino: {String(m.ratio_unavailable_reason)}</div> : null}
      </div>
      <table className="data-table">
        <tbody>
          {ROWS.filter(([k]) => k in m).map(([k, t, d]) => (
            <tr key={k}><td>{t}<Info k={k} /></td><td className="mono">{n(m[k], d)}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
