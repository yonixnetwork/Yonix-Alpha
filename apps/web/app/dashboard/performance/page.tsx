"use client";

import { useState } from "react";
import { BarChart3 } from "lucide-react";
import { Empty, ErrorNotice, Loading, PageHeader, Section, Stat } from "@/components/ui";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const DAYS = [1, 7, 30];
const COLS: [string, string][] = [
  ["trades", "Trades"], ["win_rate_pct", "Win %"], ["avg_win_pct", "Avg win %"], ["avg_loss_pct", "Avg loss %"],
  ["median_win_pct", "Median win %"], ["median_loss_pct", "Median loss %"], ["profit_factor", "Profit factor"],
  ["net_pnl_sol", "Net PnL (SOL)"], ["fees_sol", "Fees (SOL)"], ["avg_mfe_pct", "Avg MFE %"], ["avg_mae_pct", "Avg MAE %"],
  ["median_hold_s", "Median hold (s)"],
];
const v = (x: any) => (x === null || x === undefined ? "—" : String(x));
const cls = (k: string, x: any) => (["net_pnl_sol"].includes(k) && x !== null && x !== undefined
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
  const { data, error, loading } = useApi<J>("/api/analytics/solana-performance", { days }, { refreshMs: 120000 });
  const modes = Object.keys(data?.trades_by_mode ?? {});
  return (
    <div>
      <PageHeader title="Solana Performance" icon={<BarChart3 size={20} aria-hidden />}
        subtitle="PAPER and LIVE side by side: decisions, entries, closed trades by stage, hold time, entry quality and exit reason, missed winners, false positives and LIVE execution. PAPER is not evidence of LIVE results." />
      <div className="btn-row" role="group" aria-label="Window">
        {DAYS.map((d) => <button key={d} className={d === days ? "btn btn-sm" : "btn btn-ghost btn-sm"} onClick={() => setDays(d)}>{d} day{d > 1 ? "s" : ""}</button>)}
      </div>
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
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
          {modes.length === 0 && <Section title="Closed trades"><Empty>No closed Solana trades in this window.</Empty></Section>}
          {modes.map((m) => {
            const t = data.trades_by_mode[m];
            return (
              <Section key={m} title={`${m} — closed trades`}>
                <div className="stat-grid">
                  <Stat label="Trades (wins / losses)">{t.overall.trades} ({t.overall.wins} / {t.overall.losses})</Stat>
                  <Stat label="Win rate">{v(t.overall.win_rate_pct)}%</Stat>
                  <Stat label="Profit factor">{v(t.overall.profit_factor)}</Stat>
                  <Stat label="Net PnL (SOL)"><span className={cls("net_pnl_sol", t.overall.net_pnl_sol)}>{v(t.overall.net_pnl_sol)}</span></Stat>
                  <Stat label="Fees (SOL)">{v(t.overall.fees_sol)}</Stat>
                  <Stat label="Max drawdown (SOL)">{v(t.overall.max_drawdown_sol)}</Stat>
                  <Stat label="Avg MFE / MAE">{v(t.overall.avg_mfe_pct)}% / {v(t.overall.avg_mae_pct)}%</Stat>
                </div>
                {t.overall.anecdotal && <p className="muted small">Fewer than 20 trades: anecdotal, not a measured edge.</p>}
                <Breakdown title="Stage" rows={t.by_stage} />
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
          <details>
            <summary className="small">Definitions</summary>
            <ul className="small">{Object.entries(data.definitions as Record<string, string>).map(([k, d]) => <li key={k}><b>{k}</b>: {d}</li>)}</ul>
          </details>
          <p className="muted small">{data.note}</p>
        </>
      )}
    </div>
  );
}
