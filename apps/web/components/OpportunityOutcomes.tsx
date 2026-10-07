"use client";

import { useState } from "react";
import { Empty, ErrorNotice, Loading, Section, TokenLink } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type Group = Record<string, string | number | null> & { n: number };
interface Compare {
  since: string; rows: number; tracking: number; note: string; rejected_later_up_threshold_pct: string;
  winning_trades: Group; losing_trades: Group; traded: Group; rejected: Group; rejected_later_up: Group;
  loss_classes: Record<string, number>;
}
interface Opp {
  id: string; mint: string; symbol: string | null; engine: string; stage: string; decision: string; traded: boolean;
  reasons: string[]; decided_at: string; snapshot: Record<string, any>; horizons: Record<string, { change_pct?: string; unavailable?: string }>;
  peak_pct: string | null; drawdown_pct: string | null; migrated_at: string | null; status: string;
  trade_result: Record<string, any> | null; loss_analysis: Record<string, any> | null;
}

const ROWS: [string, string][] = [
  ["n", "Opportunities"], ["market_cap_sol", "Market cap (SOL)"], ["liquidity_sol", "Liquidity (SOL)"],
  ["buy_volume_sol", "Buy volume (SOL)"], ["buy_sell_ratio", "Buyers / sellers"], ["volatility", "Volatility"],
  ["entry_latency_ms", "Entry latency (ms)"], ["price_vs_decision_pct", "Price vs decision (%)"],
  ["seconds_to_migration", "Seconds to migration"], ["migrated_share", "Migrated share"],
  ["creator_launches_24h", "Creator launches 24 h"], ["top10_share", "Top-10 holder share"],
  ["signal_strength", "Signal score"], ["risk_score", "Risk score (1 low – 4 critical)"], ["ml_score", "ML score"],
  ["peak_pct", "Peak within 30 min (%)"], ["drawdown_pct", "Drawdown within 30 min (%)"],
];
const COLS: [keyof Compare, string][] = [
  ["winning_trades", "Winning trades"], ["losing_trades", "Losing trades"], ["traded", "All traded"],
  ["rejected", "Rejected"], ["rejected_later_up", "Rejected, later up"],
];
const HZ = ["T+5s", "T+10s", "T+20s", "T+30s", "T+60s", "T+5m", "T+15m", "T+30m", "T+60m"];
const pctClass = (v?: string | null) => (v == null ? "muted" : Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : "");
const fmt = (v: string | number | null | undefined) => {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  return Number.isFinite(n) && String(v).includes(".") ? String(Number(n.toFixed(4))) : String(v);
};

function Horizons({ o }: { o: Opp }) {
  return (
    <span className="hz">
      {HZ.map((h) => {
        const x = o.horizons?.[h];
        return <span key={h} title={x?.unavailable ?? h} className={x?.change_pct ? pctClass(x.change_pct) : "muted"}>
          {h.replace("T+", "")} {x?.change_pct ? `${x.change_pct}%` : x ? "n/a" : "…"}</span>;
      })}
    </span>
  );
}

/** What happened to every opportunity after the decision: winners vs losers,
 * traded vs rejected-then-up, and every losing trade's LOSS_ANALYSIS. */
export default function OpportunityOutcomes() {
  const [days, setDays] = useState(7);
  const cmp = useApi<Compare>("/api/ml/opportunities/compare", { days }, { refreshMs: 60000 });
  const losses = useApi<{ items: Opp[] }>("/api/ml/opportunities", { losses_only: true, days, limit: 20 }, { refreshMs: 60000 });
  const up = useApi<{ items: Opp[] }>("/api/ml/opportunities", { rejected_up: true, days, limit: 20 }, { refreshMs: 60000 });
  return (
    <>
      <Section title="Opportunity outcomes — traded vs not traded"
        actions={<select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="Period">
          {[1, 7, 30].map((d) => <option key={d} value={d}>last {d} day{d > 1 ? "s" : ""}</option>)}</select>}>
        {cmp.error ? <ErrorNotice error={cmp.error} /> : !cmp.data ? <Loading /> : cmp.data.rows === 0 ? (
          <Empty>No opportunities recorded yet. Every observation rejection, gate rejection/expiry and entry is recorded from now on.</Empty>
        ) : (
          <>
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr><th>At decision (averages)</th>{COLS.map(([k, l]) => <th key={k}>{l}</th>)}</tr></thead>
                <tbody>{ROWS.map(([f, label]) => (
                  <tr key={f}><td>{label}</td>{COLS.map(([k]) => <td key={k} className="mono">{fmt((cmp.data![k] as Group)?.[f])}</td>)}</tr>))}
                </tbody>
              </table>
            </div>
            <p className="muted">{cmp.data.rows} recorded ({cmp.data.tracking} still being tracked). &quot;Rejected, later up&quot; = peak
              ≥ {cmp.data.rejected_later_up_threshold_pct}% within 30 minutes of the decision. Loss classes:{" "}
              {Object.entries(cmp.data.loss_classes).map(([c, n]) => `${c} ${n}`).join(", ") || "none yet"}. {cmp.data.note}</p>
          </>
        )}
      </Section>
      <Section title="Losing trades — loss analysis">
        {losses.error ? <ErrorNotice error={losses.error} /> : !losses.data ? <Loading /> : losses.data.items.length === 0 ? (
          <Empty>No losing trade analysed yet.</Empty>
        ) : (
          <table className="data-table">
            <thead><tr><th>Token</th><th>Class</th><th>PnL</th><th>MFE / MAE</th><th>Entry latency / vs decision</th><th>Evidence</th></tr></thead>
            <tbody>{losses.data.items.map((o) => {
              const la = o.loss_analysis ?? {}; const tr = o.trade_result ?? {};
              return (
                <tr key={o.id}>
                  <td><TokenLink mint={o.mint} label={o.symbol} /><div className="muted">{formatDate(o.decided_at)}</div></td>
                  <td><span className="pill pill-warn">{la.classification}</span>
                    {(la.flags ?? []).length > 1 && <div className="muted">{(la.flags as string[]).join(", ")}</div>}</td>
                  <td className="neg mono">{fmt(tr.pnl_sol)} SOL</td>
                  <td className="mono">{fmt(tr.mfe_pct)}% / {fmt(tr.mae_pct)}%</td>
                  <td className="mono">{fmt(la.execution_latency_ms)} ms / {fmt(la.price_vs_decision_pct)}%</td>
                  <td className="muted">{(la.evidence ?? []).join("; ")}</td>
                </tr>);
            })}</tbody>
          </table>
        )}
      </Section>
      <Section title="Rejected tokens that later went up">
        {up.error ? <ErrorNotice error={up.error} /> : !up.data ? <Loading /> : up.data.items.length === 0 ? (
          <Empty>None yet.</Empty>
        ) : (
          <table className="data-table">
            <thead><tr><th>Token</th><th>Stage / decision</th><th>Why rejected</th><th>After the decision</th><th>Peak / drawdown</th></tr></thead>
            <tbody>{up.data.items.map((o) => (
              <tr key={o.id}>
                <td><TokenLink mint={o.mint} label={o.symbol} /><div className="muted">{formatDate(o.decided_at)}</div></td>
                <td>{o.stage} · {o.decision}</td>
                <td className="muted">{(o.reasons ?? []).slice(0, 2).join("; ")}</td>
                <td><Horizons o={o} /></td>
                <td className="mono"><span className="pos">+{fmt(o.peak_pct)}%</span> / <span className="neg">{fmt(o.drawdown_pct)}%</span>
                  {o.migrated_at && <div className="muted">migrated {formatDate(o.migrated_at)}</div>}</td>
              </tr>))}</tbody>
          </table>
        )}
      </Section>
    </>
  );
}
