"use client";

// One strategy: how it works, its research record, its own settings (risk,
// exits, parameters, mode), its positions, signals and decisions, and its
// performance kept apart by mode: backtest, paper, exchange demo, live.

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import EquityChart from "@/components/EquityChart";
import { errText, Info, MetricsTable, ModePill, n, StatusPill, TierPill } from "@/components/StrategyUi";
import { apiPut } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type Schema = { key: string; default: string | number; kind: string; min: string | number; max: string | number;
  common: boolean; help: string };
type Exits = { partials: { at_r: string; fraction: string; stop_to: string | null }[];
  trailing: { method: string; activate_r?: string; percent?: string; atr_mult?: string; swing_bars?: number };
  time_stop_bars: number | null; min_stop_gap_bps: string };
type Config = { enabled: boolean; mode: string; exchange: string; symbols: string[]; interval: string;
  params: Record<string, string | number>; risk: Record<string, string>; exits: Partial<Exits>; connection_id: string | null;
  version: number; updated_at: string | null; updated_by: string | null };
type Detail = {
  id: string; name: string; category: string; version: string; description: string; entry_rules: string; exit_rules: string;
  research: { family: string; designed_for: string; sources: { title: string; url: string }[]; licence: string; evidence: string;
    limitations: string; works_when: string; fails_when: string; short_rule: string; validation_tasks: string[] } | Record<string, never>;
  registry_status: string; status_reason: string; notes: string[]; status: string; why: string;
  exchanges: string[]; intervals: string[]; max_symbols: number; params_schema: Schema[];
  risk_schema: { key: string; default: string; min: string; max: string }[]; default_exits: Exits; config: Config | null;
  risk_view: { mode: string; per_trade: string; currency: string };
  practice_connections: { id: string; exchange: string; environment: string; label: string; enabled: boolean }[];
  warmup_bars: number | null;
  positions: { paper: Record<string, string>[]; exchange_demo: Record<string, string>[] };
  signals: { at: string; symbol: string; side: string; action: string; reasons: string[] }[];
  decisions: { at: string; level: string; message: string }[];
  performance: { backtest: null | { run_id: string; symbol: string; interval: string; verdict: { tier: string; reasons: string[] };
      out_of_sample: Record<string, unknown>; curve: { t: string[]; equity: number[]; drawdown_pct: number[] } | null;
      data: Record<string, unknown>; code_version: string; label: string };
    paper: Record<string, unknown>; exchange_demo: Record<string, unknown>; live: { status: string; label: string } };
  backtest_runs: { id: string; batch_id: string; status: string; symbol: string; interval: string; finished_at: string | null;
    summary: { tier?: string; oos_trades?: number; oos_expectancy_r?: number } | null; error: string | null }[];
};

const RISK_LABEL: Record<string, string> = {
  risk_per_trade_quote: "Fixed risk per trade (USDT)", risk_pct: "Risk per trade (% of equity, 0 = use the fixed amount)",
  max_position_quote: "Max position (USDT)", max_open_positions: "Max open positions (this strategy)",
  max_daily_loss_quote: "Max daily loss (USDT)", max_total_exposure_quote: "Max strategy exposure (USDT)",
  max_spread_bps: "Max spread (bps)", slippage_bps: "Slippage allowance (bps)", max_book_age_seconds: "Max price age (s)",
  max_consecutive_losses: "Pause after this many losses in a row (0 = off)", cooldown_minutes: "Pause length (minutes)",
  min_bar_volume_quote: "Min traded value of the signal candle (USDT, 0 = off)",
};

function PaperPerf({ m, title }: { m: Record<string, unknown>; title: string }) {
  return (
    <div className="sc-card">
      <div className="small"><strong>{title}</strong></div>
      <div className="muted small">{String(m.label ?? "")}</div>
      <table className="data-table" style={{ marginTop: 6 }}><tbody>
        <tr><td>Closed trades</td><td className="mono">{n(m.closed_trades, 0)}</td></tr>
        <tr><td>Open positions</td><td className="mono">{n(m.open_positions, 0)}</td></tr>
        <tr><td>Realized P&amp;L (USDT)</td><td className="mono">{n(m.realized_pnl_quote, 4)}</td></tr>
        {"unrealized_pnl_quote" in m && <tr><td>Unrealized P&amp;L</td><td className="mono">{n(m.unrealized_pnl_quote, 4)}</td></tr>}
        <tr><td>Win rate<Info k="win_rate" /></td><td className="mono">{n(m.win_rate, 3)}</td></tr>
        <tr><td>Profit factor<Info k="profit_factor" /></td><td className="mono">{n(m.profit_factor, 2)}</td></tr>
        <tr><td>Expectancy (USDT)<Info k="expectancy" /></td><td className="mono">{n(m.expectancy_quote, 4)}</td></tr>
        <tr><td>Max drawdown (USDT)</td><td className="mono">{n(m.max_drawdown_quote, 4)}</td></tr>
        <tr><td>Fees</td><td className="mono">{n(m.fees_quote, 4)}</td></tr>
      </tbody></table>
      {m.unavailable_reason ? <div className="muted small">{String(m.unavailable_reason)}</div> : null}
    </div>
  );
}

function Settings({ d, onSaved }: { d: Detail; onSaved: () => void }) {
  const c = d.config!;
  const [enabled, setEnabled] = useState(c.enabled);
  const [mode, setMode] = useState(c.mode);
  const [conn, setConn] = useState(c.connection_id ?? "");
  const [exchange, setExchange] = useState(c.exchange);
  const [symbols, setSymbols] = useState(c.symbols.join(", "));
  const [interval, setIntv] = useState(c.interval);
  const [risk, setRisk] = useState<Record<string, string>>({ ...c.risk });
  const [riskMode, setRiskMode] = useState(Number(c.risk.risk_pct ?? 0) > 0 ? "PERCENT" : "FIXED");
  const [params, setParams] = useState<Record<string, string>>(
    Object.fromEntries(d.params_schema.map((p) => [p.key, String(c.params[p.key] ?? p.default)])));
  const ex = { ...d.default_exits, ...c.exits } as Exits;
  const [trail, setTrail] = useState({ method: ex.trailing?.method ?? "none", activate_r: String(ex.trailing?.activate_r ?? "1"),
    percent: String(ex.trailing?.percent ?? "1"), atr_mult: String(ex.trailing?.atr_mult ?? "2"), swing_bars: String(ex.trailing?.swing_bars ?? "5") });
  const [partials, setPartials] = useState(ex.partials ?? []);
  const [timeStop, setTimeStop] = useState(ex.time_stop_bars ? String(ex.time_stop_bars) : "");
  const [msg, setMsg] = useState<string | null>(null);
  const conns = d.practice_connections.filter((x) => x.exchange === exchange);

  async function save() {
    const r = { ...risk };
    if (riskMode === "FIXED") r.risk_pct = "0";
    const t: Record<string, string | number> = { method: trail.method };
    if (trail.method !== "none") t.activate_r = trail.activate_r;
    if (trail.method === "percent") t.percent = trail.percent;
    if (trail.method === "atr") t.atr_mult = trail.atr_mult;
    if (trail.method === "swing") t.swing_bars = Number(trail.swing_bars);
    const p: Record<string, string | number> = {};
    for (const s of d.params_schema) p[s.key] = s.kind === "int" ? Number(params[s.key]) : params[s.key];
    try {
      await apiPut(`/api/crypto/strategies/${d.id}`, {
        expected_version: c.version, enabled, mode, exchange, interval,
        symbols: symbols.split(",").map((x) => x.trim()).filter(Boolean),
        connection_id: mode === "EXCHANGE_DEMO" ? conn : null, risk: r, params: p,
        exits: { trailing: t, partials: partials.map((x) => ({ at_r: x.at_r, fraction: x.fraction, stop_to: x.stop_to || null })),
          time_stop_bars: timeStop ? Number(timeStop) : null, min_stop_gap_bps: ex.min_stop_gap_bps ?? "20" },
      });
      setMsg("Saved.");
      onSaved();
    } catch (e) {
      setMsg(errText(e));
    }
  }

  const field = (label: string, el: React.ReactNode, help?: string) => (
    <div className="form-row"><label>{label}{help && <Info text={help} />}</label>{el}</div>
  );
  return (
    <div className="card" style={{ padding: 12 }}>
      <div className="section-title">Trading</div>
      <div className="form-grid">
        {field("Enabled", <select value={enabled ? "1" : "0"} onChange={(e) => setEnabled(e.target.value === "1")}>
          <option value="0">Off</option><option value="1">On</option></select>)}
        {field("Mode", <select value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="DISABLED">Disabled</option><option value="DEMO">Paper (simulated orders on live data)</option>
          <option value="EXCHANGE_DEMO">Exchange demo / testnet</option>
          <option value="LIVE" disabled>Live (locked)</option></select>, "Enabling a strategy never authorizes live trading.")}
        {mode === "EXCHANGE_DEMO" && field("Practice connection", <select value={conn} onChange={(e) => setConn(e.target.value)}>
          <option value="">Choose…</option>
          {conns.map((x) => <option key={x.id} value={x.id}>{x.exchange} {x.environment} &quot;{x.label}&quot;{x.enabled ? "" : " (disabled)"}</option>)}
        </select>, "Only DEMO / TESTNET connections of the same exchange are listed.")}
        {field("Exchange", <select value={exchange} onChange={(e) => setExchange(e.target.value)}>
          {d.exchanges.map((x) => <option key={x}>{x}</option>)}</select>)}
        {field(`Symbols (max ${d.max_symbols})`, <input value={symbols} onChange={(e) => setSymbols(e.target.value)} />)}
        {field("Timeframe", <select value={interval} onChange={(e) => setIntv(e.target.value)}>
          {d.intervals.map((x) => <option key={x}>{x}</option>)}</select>)}
      </div>
      <div className="section-title" style={{ marginTop: 12 }}>Risk (this strategy only; portfolio limits apply on top)</div>
      <div className="form-grid">
        {field("Risk budget", <select value={riskMode} onChange={(e) => setRiskMode(e.target.value)}>
          <option value="FIXED">Fixed amount (USDT)</option><option value="PERCENT">% of account equity</option></select>,
        "The amount lost if the stop is hit, after costs. It is not the amount invested: size = budget / stop distance.")}
        {d.risk_schema.filter((r) => !(riskMode === "FIXED" && r.key === "risk_pct") && !(riskMode === "PERCENT" && r.key === "risk_per_trade_quote"))
          .map((r) => field(RISK_LABEL[r.key] ?? r.key,
            <input value={risk[r.key] ?? r.default} inputMode="decimal"
              onChange={(e) => setRisk({ ...risk, [r.key]: e.target.value })} />, `allowed ${r.min} to ${r.max}`))}
      </div>
      <div className="section-title" style={{ marginTop: 12 }}>Exits</div>
      <div className="form-grid">
        {field("Trailing stop", <select value={trail.method} onChange={(e) => setTrail({ ...trail, method: e.target.value })}>
          <option value="none">None</option><option value="percent">Percent below the high</option>
          <option value="atr">ATR multiple below the high</option><option value="swing">Lowest low of recent bars</option></select>,
        "The stop only moves up; it never widens the approved loss.")}
        {trail.method !== "none" && field("Activate after (R)", <input value={trail.activate_r} onChange={(e) => setTrail({ ...trail, activate_r: e.target.value })} />)}
        {trail.method === "percent" && field("Percent", <input value={trail.percent} onChange={(e) => setTrail({ ...trail, percent: e.target.value })} />)}
        {trail.method === "atr" && field("ATR multiple", <input value={trail.atr_mult} onChange={(e) => setTrail({ ...trail, atr_mult: e.target.value })} />)}
        {trail.method === "swing" && field("Bars", <input value={trail.swing_bars} onChange={(e) => setTrail({ ...trail, swing_bars: e.target.value })} />)}
        {field("Time stop (bars, empty = off)", <input value={timeStop} onChange={(e) => setTimeStop(e.target.value)} />,
          "Close when this many closed bars passed and the trade is below +0.5 R.")}
      </div>
      <div className="small" style={{ marginTop: 8 }}>Partial take-profits
        <Info text="Sell a fraction at a multiple of the risk; optionally move the stop to break-even (entry + fees)." /></div>
      {partials.map((pt, i) => (
        <div key={i} className="btn-row" style={{ marginTop: 4 }}>
          <input aria-label="at R" style={{ width: 70 }} value={pt.at_r} onChange={(e) => setPartials(partials.map((x, k) => k === i ? { ...x, at_r: e.target.value } : x))} /> R ·
          sell <input aria-label="fraction" style={{ width: 70 }} value={pt.fraction} onChange={(e) => setPartials(partials.map((x, k) => k === i ? { ...x, fraction: e.target.value } : x))} />
          <label className="small"><input type="checkbox" checked={pt.stop_to === "breakeven"}
            onChange={(e) => setPartials(partials.map((x, k) => k === i ? { ...x, stop_to: e.target.checked ? "breakeven" : null } : x))} /> stop to break-even</label>
          <button className="btn btn-sm btn-ghost" onClick={() => setPartials(partials.filter((_, k) => k !== i))}>Remove</button>
        </div>
      ))}
      <button className="btn btn-sm btn-ghost" style={{ marginTop: 4 }}
        onClick={() => setPartials([...partials, { at_r: "1", fraction: "0.5", stop_to: "breakeven" }])}>Add a partial target</button>
      <div className="section-title" style={{ marginTop: 12 }}>Strategy parameters and filters</div>
      <div className="form-grid">
        {d.params_schema.map((s) => field(s.key, <input value={params[s.key]} inputMode="decimal"
          onChange={(e) => setParams({ ...params, [s.key]: e.target.value })} />, `${s.help ? s.help + "; " : ""}allowed ${s.min} to ${s.max} (default ${s.default})`))}
      </div>
      <div className="btn-row" style={{ marginTop: 12 }}>
        <button className="btn btn-sm" onClick={save}>Save settings</button>
        <span className="muted small">Version {c.version}; last change {formatDate(c.updated_at)} by {c.updated_by ?? "—"}</span>
        {msg && <span className="small">{msg}</span>}
      </div>
    </div>
  );
}

export default function StrategyDetail() {
  const { id } = useParams<{ id: string }>();
  const { data: d, error, reload } = useApi<Detail>(`/api/crypto/center/strategies/${id}`, undefined, { refreshMs: 30000 });
  const [tab, setTab] = useState("backtest");
  useEffect(() => { document.title = d ? `${d.name} · YonixAlpha` : "Strategy · YonixAlpha"; }, [d]);
  if (error) return <div className="error">{error}</div>;
  if (!d) return <div className="muted">Loading…</div>;
  const r = d.research as Detail["research"] & { sources?: { title: string; url: string }[] };
  const bt = d.performance.backtest;
  return (
    <div>
      <div className="page-header">
        <div className="page-title">{d.name}</div>
        <StatusPill status={d.status} />
        {d.config && <ModePill mode={d.config.mode} enabled={d.config.enabled} />}
      </div>
      <div className="muted small">{d.id} · v{d.version} · {d.category} · {d.why}
        {d.warmup_bars ? ` · needs ${d.warmup_bars} closed bars of history` : ""} · <Link href="/dashboard/crypto/center">all strategies</Link></div>

      <div className="section-title" style={{ marginTop: 16 }}>How it works</div>
      <p>{d.description || d.status_reason}</p>
      {d.entry_rules && <p><strong>Entry.</strong> {d.entry_rules}</p>}
      {d.exit_rules && <p><strong>Exit.</strong> {d.exit_rules}</p>}
      {d.notes.map((x) => <div key={x} className="muted small">{x}</div>)}
      {r && r.family && (
        <details>
          <summary className="small"><strong>Research record</strong> (published work and its limits; not evidence for this implementation)</summary>
          <table className="data-table small"><tbody>
            <tr><td>Family</td><td>{r.family}</td></tr><tr><td>Designed for</td><td>{r.designed_for}</td></tr>
            <tr><td>Evidence</td><td>{r.evidence}</td></tr><tr><td>Limitations</td><td>{r.limitations}</td></tr>
            <tr><td>May work when</td><td>{r.works_when}</td></tr><tr><td>Fails when</td><td>{r.fails_when}</td></tr>
            <tr><td>Short rule</td><td>{r.short_rule}</td></tr><tr><td>Code and licence</td><td>{r.licence}</td></tr>
            <tr><td>Sources</td><td>{(r.sources ?? []).map((s) => <div key={s.title}>{s.url ? <a href={s.url} target="_blank" rel="noreferrer">{s.title}</a> : s.title}</div>)}</td></tr>
            <tr><td>Validation still to do</td><td>{(r.validation_tasks ?? []).join("; ")}</td></tr>
          </tbody></table>
        </details>
      )}

      <div className="section-title" style={{ marginTop: 16 }}>Performance (each mode separately)</div>
      <div className="btn-row" role="tablist">
        {["backtest", "paper", "exchange demo", "live"].map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "btn btn-sm" : "btn btn-sm btn-ghost"}
            onClick={() => setTab(t)}>{t}</button>
        ))}
      </div>
      <div style={{ marginTop: 8 }}>
        {tab === "backtest" && (bt ? (
          <div className="perf-grid">
            <div className="sc-card">
              <div className="small">{bt.label} · {bt.symbol} {bt.interval} · <TierPill tier={bt.verdict?.tier} /></div>
              <ul className="small">{bt.verdict?.reasons.map((x) => <li key={x}>{x}</li>)}</ul>
              <MetricsTable m={bt.out_of_sample as never} label="Out-of-sample" />
              <div className="muted small">Code {bt.code_version} · data {String(bt.data?.bars ?? "")} bars from {String((bt.data?.sources as string[] | undefined)?.join(", ") ?? "")}
                · <Link href={`/dashboard/crypto/backtests?run=${bt.run_id}`}>full study</Link></div>
            </div>
            {bt.curve && <div style={{ display: "grid", gap: 12 }}>
              <EquityChart title="Out-of-sample equity (USDT)" series={[{ name: d.id, t: bt.curve.t, y: bt.curve.equity }]} />
              <EquityChart title="Drawdown (%)" unit="%" invert series={[{ name: "drawdown", t: bt.curve.t, y: bt.curve.drawdown_pct }]} height={160} />
            </div>}
          </div>
        ) : <div className="empty-state">No backtest has run for this strategy yet. Queue one on the <Link href="/dashboard/crypto/backtests">Backtests</Link> page.</div>)}
        {tab === "paper" && <PaperPerf m={d.performance.paper} title="Paper trading" />}
        {tab === "exchange demo" && <PaperPerf m={d.performance.exchange_demo} title="Exchange demo / testnet" />}
        {tab === "live" && <div className="notice">{d.performance.live.label}</div>}
      </div>

      {d.config && <>
        <div className="section-title" style={{ marginTop: 16 }}>Settings</div>
        <Settings key={d.config.version} d={d} onSaved={reload} />
      </>}

      <div className="section-title" style={{ marginTop: 16 }}>Open positions</div>
      {d.positions.paper.length + d.positions.exchange_demo.length === 0 ? <div className="empty-state">None.</div> : (
        <div className="table-scroll"><table className="data-table"><thead><tr><th>Mode</th><th>Symbol</th><th>Qty</th><th>Entry</th><th>Stop</th><th>Target</th><th>Last / status</th></tr></thead>
          <tbody>
            {d.positions.paper.map((p) => <tr key={p.id}><td>PAPER</td><td>{p.symbol}</td><td className="mono">{n(p.qty, 6)}</td><td className="mono">{n(p.entry, 4)}</td>
              <td className="mono">{n(p.stop, 4)}<div className="muted small">initial {n(p.initial_stop, 4)}</div></td><td className="mono">{n(p.target, 4)}</td><td className="mono">{n(p.last, 4)}</td></tr>)}
            {d.positions.exchange_demo.map((p) => <tr key={p.id}><td>EXCHANGE DEMO</td><td>{p.symbol}</td><td className="mono">{n(p.qty, 6)}</td><td className="mono">{n(p.entry, 4)}</td>
              <td className="mono">{n(p.stop, 4)}</td><td className="mono">{n(p.target, 4)}</td><td>{p.status} · {p.protection}</td></tr>)}
          </tbody></table></div>
      )}

      <div className="section-title" style={{ marginTop: 16 }}>Recent signals and decisions</div>
      {d.signals.length === 0 && d.decisions.length === 0 ? <div className="empty-state">No evaluation yet (the strategy is not enabled, or the engine is off).</div> : (
        <div className="table-scroll"><table className="data-table"><thead><tr><th>Candle</th><th>Symbol</th><th>Decision</th><th>Reasons</th></tr></thead>
          <tbody>
            {d.signals.map((s, i) => <tr key={i}><td className="small">{formatDate(s.at)}</td><td>{s.symbol}</td>
              <td><span className={`pill ${s.action === "REJECTED" ? "pill-danger" : s.action === "ENTER" ? "pill-ok" : "pill-off"}`}>{s.action}</span> {s.side}</td>
              <td className="small">{s.reasons.join("; ")}</td></tr>)}
            {d.decisions.map((e, i) => <tr key={`e${i}`}><td className="small">{formatDate(e.at)}</td><td /><td><span className="pill pill-off">{e.level}</span></td><td className="small">{e.message}</td></tr>)}
          </tbody></table></div>
      )}

      <div className="section-title" style={{ marginTop: 16 }}>Backtest runs</div>
      {d.backtest_runs.length === 0 ? <div className="empty-state">None yet.</div> : (
        <table className="data-table"><thead><tr><th>Run</th><th>Status</th><th>Verdict</th><th>OOS trades</th><th>OOS expectancy (R)</th><th>Finished</th></tr></thead>
          <tbody>{d.backtest_runs.map((b) => <tr key={b.id}>
            <td><Link href={`/dashboard/crypto/backtests?run=${b.id}`}>{b.symbol} {b.interval}</Link></td><td>{b.status}{b.error ? <div className="muted small">{b.error}</div> : null}</td>
            <td><TierPill tier={b.summary?.tier} /></td><td className="mono">{n(b.summary?.oos_trades, 0)}</td><td className="mono">{n(b.summary?.oos_expectancy_r, 3)}</td>
            <td className="small">{formatDate(b.finished_at)}</td></tr>)}</tbody></table>
      )}
    </div>
  );
}
