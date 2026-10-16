"use client";

// Backtests: queue a batch over the whole library, follow its progress,
// cancel it, read the ranked results and each run's full study (splits,
// walk-forward, sensitivity, benchmark, data provenance, code version and
// cost assumptions). The server runs the queue one study at a time at low
// priority; nothing here runs a backtest in the browser.

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import EquityChart from "@/components/EquityChart";
import { errText, MetricsTable, n, TierPill } from "@/components/StrategyUi";
import { apiPost } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type Batch = { id: string; status: string; total: number; counts: Record<string, number>; cancelled: boolean;
  config: { strategies: string[]; symbols: string[]; interval: string; exchange: string; start: string; end: string; optimize: boolean };
  created_at: string; created_by: string | null; finished_at: string | null; note: string | null };
type RunRow = { id: string; strategy_id: string; symbol: string; interval: string; status: string; phase: string | null;
  attempts: number; error: string | null; code_version: string | null; finished_at: string | null;
  summary: null | { tier?: string; oos_trades?: number; oos_expectancy_r?: number | null; oos_profit_factor?: number | null;
    oos_max_drawdown_pct?: number | null; oos_net_return_pct?: number | null; wf_positive_folds?: number; robust?: boolean;
    benchmark_oos_return_pct?: number | null } };
type Brief = { trades: number; net_return_pct: number; expectancy_r: number | null; profit_factor: number | null; max_drawdown_pct: number; win_rate: number | null };

function Progress({ b }: { b: Batch }) {
  const done = ["DONE", "FAILED", "CANCELLED", "SKIPPED"].reduce((a, k) => a + (b.counts[k] ?? 0), 0);
  return (
    <div title={JSON.stringify(b.counts)}>
      <div className="progress" role="progressbar" aria-valuenow={done} aria-valuemax={b.total}>
        <span style={{ width: `${b.total ? (done / b.total) * 100 : 0}%` }} /></div>
      <div className="muted small">{done}/{b.total} · {Object.entries(b.counts).map(([k, v]) => `${k.toLowerCase()} ${v}`).join(", ")}</div>
    </div>
  );
}

function RunStudy({ id }: { id: string }) {
  const { data: r, error } = useApi<{ strategy_id: string; symbol: string; interval: string; status: string; error: string | null;
    code_version: string; config: Record<string, unknown>; data: Record<string, unknown> | null;
    result: null | { splits: Record<string, { from: string; to: string; bars: number }>;
      default_params: { metrics: Record<string, Record<string, unknown>> };
      selected_on_train: { params: Record<string, unknown>; metrics: Record<string, Record<string, unknown>> };
      walk_forward: { folds: { fold: number; test: string[]; params: Record<string, unknown>; metrics: Brief }[]; positive_folds: number; pooled_trades: number };
      sensitivity: { grid: { params: Record<string, unknown>; train_validation: Brief }[]; robust: boolean; positive_points: number; points: number };
      benchmark_buy_and_hold_oos: { return_pct: number | null; max_drawdown_pct: number | null };
      verdict: { tier: string; reasons: string[]; note: string }; oos_curve: { t: string[]; equity: number[]; drawdown_pct: number[] } | null;
      traceback?: string; error?: string } }>(`/api/crypto/backtests/runs/${id}`);
  if (error) return <div className="error">{error}</div>;
  if (!r) return <div className="muted">Loading…</div>;
  const res = r.result;
  return (
    <div className="card" style={{ padding: 12, marginTop: 12 }}>
      <div className="section-title">{r.strategy_id} · {r.symbol} {r.interval} · {r.status} {res?.verdict && <TierPill tier={res.verdict.tier} />}</div>
      {r.error && <div className="error">{r.error}</div>}
      {res?.verdict && <><ul className="small">{res.verdict.reasons.map((x) => <li key={x}>{x}</li>)}</ul><div className="muted small">{res.verdict.note}</div></>}
      {res?.splits && (
        <>
          <div className="perf-grid" style={{ marginTop: 8 }}>
            {(["train", "validation", "out_of_sample"] as const).map((k) => (
              <div key={k} className="sc-card"><MetricsTable m={res.default_params.metrics[k] as never}
                label={`${k.replace(/_/g, " ")} · default parameters · ${res.splits[k].bars} bars`} /></div>
            ))}
          </div>
          {res.oos_curve && <div style={{ marginTop: 12 }}>
            <EquityChart title="Out-of-sample equity, default parameters (USDT)" series={[{ name: r.strategy_id, t: res.oos_curve.t, y: res.oos_curve.equity }]} /></div>}
          <div className="section-title" style={{ marginTop: 12 }}>Benchmark (buy and hold, out-of-sample)</div>
          <div className="small">Return {n(res.benchmark_buy_and_hold_oos.return_pct)}% · max drawdown {n(res.benchmark_buy_and_hold_oos.max_drawdown_pct)}%</div>
          <div className="section-title" style={{ marginTop: 12 }}>Parameters chosen on the training period only</div>
          <div className="small mono">{JSON.stringify(res.selected_on_train.params)}</div>
          <table className="data-table small"><thead><tr><th>Period</th><th>Trades</th><th>Expectancy (R)</th><th>PF</th><th>Max DD %</th></tr></thead>
            <tbody>{Object.entries(res.selected_on_train.metrics).map(([k, m]) => (
              <tr key={k}><td>{k.replace(/_/g, " ")}</td><td className="mono">{n(m.trades, 0)}</td><td className="mono">{n(m.expectancy_r, 3)}</td>
                <td className="mono">{n(m.profit_factor)}</td><td className="mono">{n(m.max_drawdown_pct)}</td></tr>))}</tbody></table>
          <div className="section-title" style={{ marginTop: 12 }}>Walk-forward ({res.walk_forward.positive_folds}/{res.walk_forward.folds.length} folds positive, {res.walk_forward.pooled_trades} trades)</div>
          <table className="data-table small"><thead><tr><th>Fold</th><th>Test period</th><th>Params (from the earlier data)</th><th>Trades</th><th>Expectancy (R)</th><th>Max DD %</th></tr></thead>
            <tbody>{res.walk_forward.folds.map((f) => (
              <tr key={f.fold}><td>{f.fold}</td><td>{f.test[0].slice(0, 10)} to {f.test[1].slice(0, 10)}</td><td className="mono">{JSON.stringify(f.params)}</td>
                <td className="mono">{n(f.metrics.trades, 0)}</td><td className="mono">{n(f.metrics.expectancy_r, 3)}</td><td className="mono">{n(f.metrics.max_drawdown_pct)}</td></tr>))}</tbody></table>
          <div className="section-title" style={{ marginTop: 12 }}>Sensitivity ({res.sensitivity.positive_points}/{res.sensitivity.points} grid points positive on train + validation; {res.sensitivity.robust ? "robust" : "not robust"})</div>
          <table className="data-table small"><thead><tr><th>Params</th><th>Trades</th><th>Expectancy (R)</th><th>PF</th><th>Max DD %</th></tr></thead>
            <tbody>{res.sensitivity.grid.map((g, i) => (
              <tr key={i}><td className="mono">{JSON.stringify(g.params)}</td><td className="mono">{n(g.train_validation.trades, 0)}</td>
                <td className="mono">{n(g.train_validation.expectancy_r, 3)}</td><td className="mono">{n(g.train_validation.profit_factor)}</td>
                <td className="mono">{n(g.train_validation.max_drawdown_pct)}</td></tr>))}</tbody></table>
        </>
      )}
      <details style={{ marginTop: 12 }}><summary className="small">Reproducibility: code version, data and assumptions</summary>
        <pre className="small mono" style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify({ code_version: r.code_version, config: r.config, data: r.data }, null, 2)}</pre>
        {res?.traceback && <pre className="small mono" style={{ whiteSpace: "pre-wrap" }}>{res.traceback}</pre>}
      </details>
    </div>
  );
}

function NewBatch({ onDone }: { onDone: () => void }) {
  const today = new Date().toISOString().slice(0, 10);
  const [f, setF] = useState({ symbols: "BTC/USDT, ETH/USDT, SOL/USDT", interval: "1h", exchange: "binance_us",
    start: `${Number(today.slice(0, 4)) - 2}${today.slice(4)}`, end: today, taker: "2", maker: "0", spread: "2", slippage: "5",
    riskMode: "PERCENT", risk: "0.5", equity: "10000", optimize: true, note: "" });
  const [msg, setMsg] = useState<string | null>(null);
  async function go() {
    try {
      const r = await apiPost<{ batch_id: string; runs: number; backtests_enabled: boolean; worker_hint: string }>("/api/crypto/backtests", {
        strategies: "all", symbols: f.symbols.split(",").map((x) => x.trim()).filter(Boolean), interval: f.interval, exchange: f.exchange,
        start: `${f.start}T00:00:00Z`, end: `${f.end}T00:00:00Z`, optimize: f.optimize, note: f.note || null,
        costs: { taker_fee_bps: Number(f.taker), maker_fee_bps: Number(f.maker), spread_bps: Number(f.spread), slippage_bps: Number(f.slippage) },
        risk: { mode: f.riskMode, value: Number(f.risk), starting_equity: Number(f.equity) } });
      setMsg(`Queued ${r.runs} runs.${r.backtests_enabled ? "" : " Backtests are switched off on the Portfolio risk page: the worker will not run them until you switch them on."} ${r.worker_hint}`);
      onDone();
    } catch (e) {
      setMsg(errText(e));
    }
  }
  const inp = (k: keyof typeof f, label: string, type = "text") => (
    <div className="form-row"><label>{label}</label><input type={type} value={String(f[k])} onChange={(e) => setF({ ...f, [k]: e.target.value })} /></div>);
  return (
    <div className="card" style={{ padding: 12 }}>
      <div className="small" style={{ marginBottom: 8 }}>Runs every library strategy on every symbol. Costs default to Binance.US&apos;s
        published spot fees (0 maker / 2 bps taker, an assumption, not account-verified) plus spread and slippage.</div>
      <div className="form-grid">
        {inp("symbols", "Symbols")}
        <div className="form-row"><label>Timeframe</label><select value={f.interval} onChange={(e) => setF({ ...f, interval: e.target.value })}>
          {["15m", "1h", "4h", "1d"].map((x) => <option key={x}>{x}</option>)}</select></div>
        <div className="form-row"><label>Data (public candles)</label><select value={f.exchange} onChange={(e) => setF({ ...f, exchange: e.target.value })}>
          <option value="binance_us">Binance.US (reachable from the server)</option><option value="bybit">Bybit</option><option value="binance">Binance</option></select></div>
        {inp("start", "From", "date")}{inp("end", "To", "date")}
        {inp("taker", "Taker fee (bps)")}{inp("maker", "Maker fee (bps)")}{inp("spread", "Spread (bps)")}{inp("slippage", "Slippage (bps)")}
        <div className="form-row"><label>Risk per trade</label><select value={f.riskMode} onChange={(e) => setF({ ...f, riskMode: e.target.value })}>
          <option value="PERCENT">% of equity</option><option value="FIXED">Fixed USDT</option></select></div>
        {inp("risk", f.riskMode === "PERCENT" ? "Risk %" : "Risk USDT")}{inp("equity", "Starting equity (USDT)")}
        <div className="form-row"><label>Parameter grid (train only)</label><select value={f.optimize ? "1" : "0"} onChange={(e) => setF({ ...f, optimize: e.target.value === "1" })}>
          <option value="1">On (3 x 3 grid, walk-forward)</option><option value="0">Off (default parameters only)</option></select></div>
        {inp("note", "Note")}
      </div>
      <div className="btn-row" style={{ marginTop: 8 }}><button className="btn btn-sm" onClick={go}>Queue the batch</button>{msg && <span className="small">{msg}</span>}</div>
    </div>
  );
}

function Backtests() {
  const params = useSearchParams();
  const runParam = params.get("run");
  const list = useApi<{ batches: Batch[]; backtests_enabled: boolean; optimization_enabled: boolean }>("/api/crypto/backtests", undefined, { refreshMs: 10000 });
  const [open, setOpen] = useState<string | null>(null);
  const [run, setRun] = useState<string | null>(runParam);
  const batch = useApi<Batch & { runs: RunRow[] }>(open ? `/api/crypto/backtests/${open}` : null, undefined, { refreshMs: 10000 });
  const [msg, setMsg] = useState<string | null>(null);
  async function cancel(id: string) {
    if (!window.confirm("Cancel this batch? Queued runs are cancelled; a run in progress finishes its current study.")) return;
    try { await apiPost(`/api/crypto/backtests/${id}/cancel`); list.reload(); } catch (e) { setMsg(errText(e)); }
  }
  return (
    <div>
      <div className="page-header"><div className="page-title">Crypto Trading · Backtests</div></div>
      <div className="notice">
        Simulations on historical public candles: the signal bar never fills (next open), the stop is assumed first when a
        bar touches both levels, fills are capped by volume, and fees, spread and slippage are charged. Parameters are
        only ever chosen on the training period. A backtest is not evidence that a strategy will make money.
      </div>
      {list.data && !list.data.backtests_enabled && <div className="notice notice-warn">Backtests are switched off. Turn them on
        under <Link href="/dashboard/crypto/portfolio">Portfolio risk</Link>; queued batches wait until then.</div>}
      {msg && <div className="error">{msg}</div>}
      {run && <><div className="btn-row" style={{ marginTop: 8 }}><button className="btn btn-sm btn-ghost" onClick={() => setRun(null)}>Close the study</button></div><RunStudy id={run} /></>}
      <div className="section-title" style={{ marginTop: 16 }}>New batch</div>
      <NewBatch onDone={list.reload} />
      <div className="section-title" style={{ marginTop: 16 }}>Batches</div>
      {list.error && <div className="error">{list.error}</div>}
      {list.data && list.data.batches.length === 0 && <div className="empty-state">No batch yet.</div>}
      {list.data && list.data.batches.length > 0 && (
        <div className="table-scroll"><table className="data-table"><thead><tr><th>Created</th><th>Scope</th><th>Status</th><th>Progress</th><th /></tr></thead>
          <tbody>{list.data.batches.map((b) => (
            <tr key={b.id} className="clickable" onClick={() => setOpen(open === b.id ? null : b.id)}>
              <td className="small">{formatDate(b.created_at)}<div className="muted">{b.created_by}</div></td>
              <td className="small">{b.config.strategies.length} strategies · {b.config.symbols.join(", ")} · {b.config.interval} · {b.config.exchange}
                <div className="muted">{b.config.start.slice(0, 10)} to {b.config.end.slice(0, 10)}{b.note ? ` · ${b.note}` : ""}</div></td>
              <td>{b.status}</td><td style={{ minWidth: 180 }}><Progress b={b} /></td>
              <td>{!b.cancelled && b.status !== "DONE" && <button className="btn btn-sm btn-ghost" onClick={(e) => { e.stopPropagation(); cancel(b.id); }}>Cancel</button>}</td>
            </tr>))}</tbody></table></div>
      )}
      {open && batch.data && (
        <>
          <div className="section-title" style={{ marginTop: 16 }}>Ranked results (documented rule; win rate not used)</div>
          <div className="table-scroll"><table className="data-table"><thead><tr><th>Strategy</th><th>Symbol</th><th>Status</th><th>Verdict</th>
            <th>OOS trades</th><th>OOS expectancy (R)</th><th>OOS PF</th><th>OOS max DD %</th><th>OOS return %</th><th>Buy and hold %</th><th>WF folds +</th><th>Robust</th></tr></thead>
            <tbody>{batch.data.runs.map((r) => (
              <tr key={r.id} className="clickable" onClick={() => setRun(r.id)}>
                <td><Link href={`/dashboard/crypto/center/${r.strategy_id}`} onClick={(e) => e.stopPropagation()}>{r.strategy_id}</Link></td>
                <td>{r.symbol}</td><td className="small">{r.status}{r.phase ? ` · ${r.phase}` : ""}{r.error ? <div className="muted">{r.error}</div> : null}</td>
                <td><TierPill tier={r.summary?.tier} /></td><td className="mono">{n(r.summary?.oos_trades, 0)}</td>
                <td className="mono">{n(r.summary?.oos_expectancy_r, 3)}</td><td className="mono">{n(r.summary?.oos_profit_factor)}</td>
                <td className="mono">{n(r.summary?.oos_max_drawdown_pct)}</td><td className="mono">{n(r.summary?.oos_net_return_pct)}</td>
                <td className="mono">{n(r.summary?.benchmark_oos_return_pct)}</td><td className="mono">{n(r.summary?.wf_positive_folds, 0)}</td>
                <td>{r.summary?.robust == null ? "n/a" : r.summary.robust ? "yes" : "no"}</td>
              </tr>))}</tbody></table></div>
        </>
      )}
    </div>
  );
}

export default function BacktestsPage() {
  return <Suspense fallback={<div className="muted">Loading…</div>}><Backtests /></Suspense>;
}
