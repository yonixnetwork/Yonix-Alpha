"use client";

import { useState } from "react";
import { BarChart3 } from "lucide-react";
import { Empty, ErrorNotice, Loading, PageHeader, ReviewStatus, Section, Stat, reviewReady } from "@/components/ui";
import { apiGet } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import ForwardTestPanel from "@/components/ForwardTestPanel";
import SolanaPositionsTable from "@/components/SolanaPositionsTable";

type J = Record<string, any>;
const DAYS = [1, 7, 30, 90];
const ENGINES: [string, string][] = [["", "All strategies"], ["solana_momentum", "Momentum"], ["solana_fresh", "Fresh"],
  ["solana_migration", "Migration"]];
const COLS: [string, string][] = [
  ["trades", "Closed"], ["wins", "W"], ["losses", "L"], ["breakeven", "BE"], ["win_rate_pct", "Win %"],
  ["avg_win_pct", "Avg win %"], ["avg_loss_pct", "Avg loss %"], ["median_win_pct", "Median win %"],
  ["median_loss_pct", "Median loss %"], ["profit_factor", "Profit factor"], ["gross_pnl_sol", "Gross (SOL)"],
  ["fees_sol", "Fees (SOL)"], ["net_pnl_sol", "Net PnL (SOL)"], ["expectancy_sol", "Net per trade"],
  ["avg_mfe_pct", "Avg MFE %"], ["avg_mae_pct", "Avg MAE %"], ["median_hold_s", "Median hold (s)"],
];
const v = (x: any) => (x === null || x === undefined ? "—" : String(x));
const cls = (k: string, x: any) => (["net_pnl_sol", "expectancy_sol", "gross_pnl_sol"].includes(k) && x !== null && x !== undefined
  ? (Number(x) > 0 ? "pos" : Number(x) < 0 ? "neg" : "") : "");

function Breakdown({ title, rows }: { title: string; rows: Record<string, J> | undefined }) {
  const keys = Object.keys(rows ?? {});
  if (!keys.length) return null;
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead><tr><th>{title}</th>{COLS.map(([, l]) => <th key={l}>{l}</th>)}</tr></thead>
        <tbody>{keys.map((k) => (
          <tr key={k}><td>{k}{rows![k].anecdotal && <span className="muted small" title="fewer than 20 trades"> (small sample)</span>}</td>
            {COLS.map(([c]) => <td key={c} className={cls(c, rows![k][c])}>{v(rows![k][c])}</td>)}</tr>))}</tbody>
      </table>
    </div>
  );
}

/** Solana PAPER vs LIVE (apps/api /analytics/solana-performance,
 * yonixalpha_core.solana_performance): measured from stored rows only. */
export default function SolanaPerformancePage() {
  const [days, setDays] = useState(7);
  const [engine, setEngine] = useState("");
  const params = { days, engine: engine || undefined };
  const q = useApi<J>("/api/analytics/solana-performance", params, { refreshMs: 60000 });
  const data = q.data && reviewReady(q.data) ? q.data : null;
  const refresh = () => { void apiGet("/api/analytics/solana-performance", { ...params, refresh: true }).catch(() => undefined).then(() => q.reload()); };
  const modes = Object.keys(data?.trades_by_mode ?? {});
  return (
    <div>
      <PageHeader title="Solana Performance" icon={<BarChart3 size={20} aria-hidden />}
        subtitle="PAPER and LIVE side by side: decisions, entries, closed trades by stage, hold time, entry quality and exit reason, missed winners, false positives and LIVE execution. PAPER is not evidence of LIVE results." />
      <ForwardTestPanel />
      <div className="btn-row" role="group" aria-label="Window">
        {DAYS.map((d) => <button key={d} className={d === days ? "btn btn-sm" : "btn btn-ghost btn-sm"} onClick={() => setDays(d)}>{d} day{d > 1 ? "s" : ""}</button>)}
        <select aria-label="Strategy" value={engine} onChange={(e) => setEngine(e.target.value)}>
          {ENGINES.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </select>
      </div>
      <ErrorNotice error={q.error} />
      {q.data && !data && <ReviewStatus data={q.data} onRefresh={refresh} />}
      {q.loading && !q.data && <Loading />}
      {data && (
        <>
          <Section title="Decisions and entries">
            <div className="stat-grid">
              {Object.entries(data.decisions_by_target as Record<string, J>).map(([t, x]) => (
                <Stat key={t} label={`${t}: signals / qualified / rejected`}>{x.signals} / {x.qualified} / {x.rejected}</Stat>))}
              {Object.entries(data.entries_by_mode as Record<string, J>).map(([m, x]) => (
                <Stat key={m} label={`${m}: entries / exited / still open`}>{x.entries} / {x.exited} / {x.still_open}</Stat>))}
            </div>
            {!Object.keys(data.decisions_by_target).length && !Object.keys(data.entries_by_mode).length && <Empty>No Solana decisions or entries in this window.</Empty>}
          </Section>
          {(data.open_positions ?? []).length > 0 && (
            <Section title="Open positions (not booked)">
              <p className="muted small">Open positions are never part of any PnL above.</p>
              <ul className="small">{data.open_positions.map((o: J, i: number) => <li key={i}>{o.mode} {o.engine}: {o.count} {o.status}, cost {v(o.cost_sol)} SOL, oldest entry {v(o.oldest_entry)}</li>)}</ul>
            </Section>)}
          {modes.length === 0 && <Section title="Closed trades"><Empty>No closed Solana trades in this window.</Empty></Section>}
          {modes.map((m) => {
            const t = data.trades_by_mode[m];
            return (
              <Section key={m} title={`${m} — closed trades`}>
                <div className="stat-grid">
                  <Stat label="Closed (wins / losses / breakeven)">{t.overall.trades} ({t.overall.wins} / {t.overall.losses} / {t.overall.breakeven})</Stat>
                  <Stat label="Win rate / loss rate">{v(t.overall.win_rate_pct)}% / {v(t.overall.loss_rate_pct)}%</Stat>
                  <Stat label="Profit factor" hint={t.overall.profit_factor_note ?? undefined}>{v(t.overall.profit_factor)}</Stat>
                  <Stat label="Gross (SOL)" hint="net + fees: before the recorded costs">{v(t.overall.gross_pnl_sol)}</Stat>
                  <Stat label="Fees (SOL)" hint="program, AMM, network, priority and close costs; counted once">{v(t.overall.fees_sol)}</Stat>
                  <Stat label="Net PnL (SOL)"><span className={cls("net_pnl_sol", t.overall.net_pnl_sol)}>{v(t.overall.net_pnl_sol)}</span></Stat>
                  <Stat label="Net per trade (SOL)"><span className={cls("expectancy_sol", t.overall.expectancy_sol)}>{v(t.overall.expectancy_sol)}</span></Stat>
                  <Stat label="Largest win / loss (SOL)">{v(t.overall.largest_win_sol)} / {v(t.overall.largest_loss_sol)}</Stat>
                  <Stat label="Max drawdown (SOL)">{v(t.overall.max_drawdown_sol)}</Stat>
                  <Stat label="Return on starting capital" hint={t.overall.return_note ?? "net / the paper account's starting balance"}>{v(t.overall.return_on_starting_capital_pct)}%</Stat>
                  <Stat label="Avg open at once / trades per day">{v(t.overall.avg_open_positions)} / {v(t.overall.trades_per_day)}</Stat>
                  <Stat label="Avg MFE / MAE">{v(t.overall.avg_mfe_pct)}% / {v(t.overall.avg_mae_pct)}%</Stat>
                </div>
                {t.overall.anecdotal && <p className="muted small">Fewer than 20 trades: anecdotal, not a measured edge.</p>}
                {data.coverage?.[m] && <p className="muted small">Data coverage: {data.coverage[m].closed} closed; no ledger record {data.coverage[m].no_ledger_record}, fees not recorded {data.coverage[m].fees_not_recorded}, token age unknown {data.coverage[m].age_unknown}, market activity unknown {data.coverage[m].activity_unknown}, entry quality unknown {data.coverage[m].entry_quality_unknown}.</p>}
                <Breakdown title="Day (UTC)" rows={t.by_day} />
                <Breakdown title="Strategy" rows={t.by_engine} />
                <Breakdown title="Stage" rows={t.by_stage} />
                <Breakdown title="Token age" rows={t.by_age} />
                <Breakdown title="Market activity" rows={t.by_regime} />
                <Breakdown title="Hold time" rows={t.by_hold} />
                <Breakdown title="Entry quality" rows={t.by_entry_quality} />
                <Breakdown title="Exit reason" rows={t.by_exit_reason} />
              </Section>
            );
          })}
          <Section title="Missed winners and false positives">
            {(data.missed_winners_by_rule ?? []).length === 0 ? <Empty>No missed winners classified in this window.</Empty> : (
              <div className="table-scroll"><table className="data-table">
                <thead><tr><th>Rejected by</th><th>Missed winners</th><th>Median peak %</th></tr></thead>
                <tbody>{data.missed_winners_by_rule.map((r: J) => <tr key={r.rule}><td>{r.rule}</td><td>{r.count}</td><td>{v(r.median_peak_pct)}</td></tr>)}</tbody>
              </table></div>)}
            {Object.entries((data.false_positives_by_mode ?? {}) as Record<string, J>).map(([m, x]) => (
              <p key={m} className="small">{m} false positives (entered, lost): {Object.entries(x).map(([k, n]) => `${k} ${n}`).join(" · ")}</p>))}
            {Object.entries((data.exit_timing_by_mode ?? {}) as Record<string, J>).map(([m, x]) => (
              <p key={m} className="small">{m} exit timing: {Object.entries(x).map(([k, n]) => `${k} ${n}`).join(" · ")}</p>))}
          </Section>
          <Section title="LIVE execution">
            {data.execution_live.length === 0 ? <Empty>No LIVE orders in this window.</Empty> : (
              <div className="table-scroll"><table className="data-table">
                <thead><tr><th>Side</th><th>Route</th><th>Status</th><th>Orders</th><th>Decision to submit (ms)</th>
                  <th>Decision to confirm (ms)</th><th>Slippage vs expected %</th><th>All-in vs decision %</th></tr></thead>
                <tbody>{data.execution_live.map((r: J, i: number) => (
                  <tr key={i}><td>{r.side}</td><td>{r.route}</td><td>{r.status}</td><td>{r.orders}</td>
                    <td>{v(r.median_decision_to_submit_ms)}</td><td>{v(r.median_decision_to_confirm_ms)}</td>
                    <td>{v(r.median_slippage_vs_expected_pct)}</td><td>{v(r.median_total_vs_decision_pct)}</td></tr>))}</tbody>
              </table></div>)}
          </Section>
          <SolanaPositionsTable engine={engine} />
          <details>
            <summary className="small">Definitions</summary>
            <ul className="small">{Object.entries(data.definitions as Record<string, string>).map(([k, d]) => <li key={k}><b>{k}</b>: {d}</li>)}</ul>
          </details>
          <p className="muted small">{data.note}</p>
          <ReviewStatus data={data} onRefresh={refresh} />
        </>
      )}
    </div>
  );
}
