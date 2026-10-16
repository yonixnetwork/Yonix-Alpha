"use client";

// Compare strategies: pick runs from one batch (same data period and cost
// assumptions), overlay their out-of-sample equity curves, compare metrics,
// and see return correlation and time-in-market overlap, which reveal
// duplicate strategies and concentrated risk. Current combined exposure is
// shown from the live portfolio state.

import Link from "next/link";
import { useState } from "react";
import EquityChart from "@/components/EquityChart";
import { Info, n, TierPill } from "@/components/StrategyUi";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type Batch = { id: string; created_at: string; status: string; config: { symbols: string[]; interval: string; start: string; end: string } };
type RunRow = { id: string; strategy_id: string; symbol: string; status: string; summary: { tier?: string } | null };
type Item = { run_id: string; strategy_id: string; symbol: string; summary: { tier?: string };
  oos: Record<string, number | null> | null; curve: { t: string[]; equity: number[] } | null };
type Cmp = { items: Item[]; correlation: Record<string, Record<string, number | null>>; overlap: Record<string, Record<string, number | null>>;
  same_period_and_costs: boolean; notes: string[];
  current_exposure: { open_positions: number; open_risk_quote: string; total_exposure_quote: string; exposure_per_asset_quote: Record<string, string> } };

const ROWS: [string, string, number][] = [["trades", "Trades", 0], ["net_return_pct", "Net return %", 2], ["expectancy_r", "Expectancy (R)", 3],
  ["profit_factor", "Profit factor", 2], ["max_drawdown_pct", "Max drawdown %", 2], ["sharpe", "Sharpe", 2], ["win_rate", "Win rate", 3],
  ["exposure_pct", "Exposure %", 1], ["fees", "Fees", 2]];

function Matrix({ title, m, items, info }: { title: string; m: Record<string, Record<string, number | null>>; items: Item[]; info: string }) {
  return (
    <div className="sc-card">
      <div className="small"><strong>{title}</strong><Info text={info} /></div>
      <div className="table-scroll"><table className="data-table small"><thead><tr><th />{items.map((i) => <th key={i.run_id}>{i.strategy_id}</th>)}</tr></thead>
        <tbody>{items.map((a) => (
          <tr key={a.run_id}><td>{a.strategy_id} <span className="muted">{a.symbol}</span></td>
            {items.map((b) => <td key={b.run_id} className="mono">{a.run_id === b.run_id ? "—" : n(m[a.run_id]?.[b.run_id], 2)}</td>)}</tr>))}</tbody></table></div>
    </div>
  );
}

export default function Compare() {
  const batches = useApi<{ batches: Batch[] }>("/api/crypto/backtests");
  const [batch, setBatch] = useState("");
  const runs = useApi<{ runs: RunRow[] }>(batch ? `/api/crypto/backtests/${batch}` : null);
  const [picked, setPicked] = useState<string[]>([]);
  const cmp = useApi<Cmp>(picked.length >= 2 ? `/api/crypto/compare` : null, { runs: picked.join(",") });
  const done = (runs.data?.runs ?? []).filter((r) => r.status === "DONE");
  return (
    <div>
      <div className="page-header"><div className="page-title">Crypto Trading · Compare strategies</div></div>
      <div className="notice">Compare runs from one batch, so they share the same data period and cost assumptions. Paper and
        exchange-demo results are on each strategy&apos;s page and are never mixed with backtests.</div>
      <div className="strategy-filters">
        <select aria-label="Batch" value={batch} onChange={(e) => { setBatch(e.target.value); setPicked([]); }}>
          <option value="">Choose a batch…</option>
          {(batches.data?.batches ?? []).map((b) => <option key={b.id} value={b.id}>{formatDate(b.created_at)} · {b.config.symbols.join(", ")} {b.config.interval} · {b.status}</option>)}
        </select>
      </div>
      {batch && done.length === 0 && <div className="empty-state">No finished runs in this batch yet.</div>}
      {done.length > 0 && (
        <div className="btn-row" style={{ flexWrap: "wrap" }}>
          {done.map((r) => (
            <label key={r.id} className="small" style={{ marginRight: 12 }}>
              <input type="checkbox" checked={picked.includes(r.id)} disabled={!picked.includes(r.id) && picked.length >= 8}
                onChange={(e) => setPicked(e.target.checked ? [...picked, r.id] : picked.filter((x) => x !== r.id))} /> {r.strategy_id} {r.symbol}
              {" "}<TierPill tier={r.summary?.tier} />
            </label>
          ))}
          <span className="muted small">pick 2 to 8</span>
        </div>
      )}
      {cmp.error && <div className="error">{cmp.error}</div>}
      {cmp.data && (
        <>
          {!cmp.data.same_period_and_costs && <div className="notice notice-warn">These runs used different periods or costs: not comparable.</div>}
          <div style={{ marginTop: 12 }}>
            <EquityChart title="Out-of-sample equity (USDT)" series={cmp.data.items.filter((i) => i.curve).map((i) => ({
              name: `${i.strategy_id} ${i.symbol}`, t: i.curve!.t, y: i.curve!.equity }))} />
          </div>
          <div className="table-scroll" style={{ marginTop: 12 }}><table className="data-table"><thead><tr><th>Metric</th>
            {cmp.data.items.map((i) => <th key={i.run_id}>{i.strategy_id}<div className="muted small">{i.symbol}</div></th>)}</tr></thead>
            <tbody>
              <tr><td>Verdict</td>{cmp.data.items.map((i) => <td key={i.run_id}><TierPill tier={i.summary?.tier} /></td>)}</tr>
              {ROWS.map(([k, t, d]) => <tr key={k}><td>{t}<Info k={k} /></td>{cmp.data!.items.map((i) => <td key={i.run_id} className="mono">{n(i.oos?.[k], d)}</td>)}</tr>)}
            </tbody></table></div>
          <div className="perf-grid" style={{ marginTop: 12 }}>
            <Matrix title="Return correlation" m={cmp.data.correlation} items={cmp.data.items}
              info="Correlation of daily out-of-sample returns (n/a with fewer than 20 common days). Near 1 = the strategies move together: little diversification." />
            <Matrix title="Time-in-market overlap" m={cmp.data.overlap} items={cmp.data.items}
              info="Share of the row strategy's time in the market during which the column strategy also held the same symbol. High values mean duplicated exposure." />
          </div>
          <ul className="muted small">{cmp.data.notes.map((x) => <li key={x}>{x}</li>)}</ul>
          <div className="section-title">Current combined exposure (all modes)</div>
          <div className="small">{cmp.data.current_exposure.open_positions} open positions · open risk {n(cmp.data.current_exposure.open_risk_quote)} USDT ·
            exposure {n(cmp.data.current_exposure.total_exposure_quote)} USDT
            {Object.entries(cmp.data.current_exposure.exposure_per_asset_quote).map(([k, v]) => ` · ${k} ${n(v)}`)} ·
            limits on <Link href="/dashboard/crypto/portfolio">Portfolio risk</Link></div>
        </>
      )}
    </div>
  );
}
