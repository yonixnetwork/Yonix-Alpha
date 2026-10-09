"use client";

import { Fragment, useState } from "react";
import {
  Activity, AlertTriangle, Ban, CheckCircle2, Clock, Eye, FlaskConical, LogOut, RefreshCcw, ShieldCheck, Target, TrendingUp, XCircle,
} from "lucide-react";
import MarketCap from "@/components/MarketCap";
import { Empty, ErrorNotice, Loading, ReviewStatus, Section, Stat, TokenLink, reviewReady } from "@/components/ui";
import { apiGet } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
export interface LedgerRow {
  id: string; mint: string; symbol: string | null; engine: string; stage: string; decision: string; traded: boolean;
  reasons: string[]; decided_at: string; status: string; snapshot: J; horizons: J; path: J | null; analysis: J | null;
  labels: J | null; regime: J | null; post_exit: J | null; ml_shadow: J | null; trade_result: J | null;
  peak_pct: string | null; drawdown_pct: string | null; theoretical_return_pct: string | null; executable_return_pct: string | null;
}
interface Review {
  since: string; counts: Record<string, number>; categories: string[]; note: string;
  missed_win_buckets: Record<string, Record<string, number>>;
  signal_vs_execution: J; snipe_latency: Record<string, { n: number; median: number | null; p90: number | null }>;
  shadow_models: J[]; cached_at?: string; age_s?: number; review_status?: string; refreshing?: boolean;
  deferred?: string; refresh_error?: string; error?: string; message?: string;
}

export const PATH_HORIZONS = ["T+5s", "T+10s", "T+20s", "T+30s", "T+60s", "T+5m", "T+15m", "T+30m", "T+60m"];
const CATS: [string, string, typeof Eye][] = [
  ["observed", "Observed", Eye], ["traded", "Traded", Activity], ["rejected", "Rejected", Ban],
  ["missed_win", "Missed winners", Target], ["rejection_justified_drawdown", "Rejection justified (drawdown first)", ShieldCheck],
  ["correct_rejection", "Correct rejections", CheckCircle2], ["unexecutable", "Unexecutable after costs", XCircle],
  ["counterfactual_unknown", "Counterfactual unknown", AlertTriangle], ["true_positive", "Winning trades", TrendingUp],
  ["false_positive", "False positives (losing trades)", AlertTriangle], ["premature_exit", "Possibly early exits", LogOut],
  ["late_exit", "Possibly late exits", Clock], ["good_exit", "Good exits", CheckCircle2],
  ["risk_correct_exit", "Risk-correct exits", ShieldCheck], ["recovery", "Recovered after a drawdown", RefreshCcw],
  ["tracking", "Still tracking", Clock],
];
const n = (v: unknown) => (v === null || v === undefined || v === "" ? null : Number(v));
const pct = (v: unknown) => {
  const x = n(v);
  return x === null || !Number.isFinite(x) ? "—" : `${x > 0 ? "+" : ""}${x.toFixed(1)}%`;
};
const cls = (v: unknown) => { const x = n(v); return x === null ? "muted" : x > 0 ? "pos" : x < 0 ? "neg" : ""; };

/** The path after a decision, T0 → T+60m: price change, running drawdown,
 * flow per interval, and the executable return of a reference-size trade. */
export function PathView({ row }: { row: LedgerRow }) {
  const path = row.path ?? {};
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead><tr><th>Horizon</th><th>Change</th><th>Peak / drawdown so far</th><th>Market cap</th><th>Buys / sells</th>
          <th>Buyers / sellers</th><th>Executable (theoretical)</th></tr></thead>
        <tbody>{PATH_HORIZONS.map((h) => {
          const p = path[h];
          if (!p) return <tr key={h}><td>{h}</td><td colSpan={6} className="muted">{row.horizons?.[h]?.unavailable ?? "not reached yet"}</td></tr>;
          const ex = p.executable ?? {};
          return (
            <tr key={h}>
              <td>{h}</td>
              <td className={cls(p.change_pct)}>{p.unknown ? <span className="muted">{p.unknown}</span> : pct(p.change_pct)}</td>
              <td className="mono"><span className="pos">{pct(p.peak_so_far_pct)}</span> / <span className="neg">{pct(p.drawdown_so_far_pct)}</span></td>
              <td className="mono"><MarketCap sol={p.market_cap_sol} historical compact /></td>
              <td className="mono">{p.buys ?? "—"} / {p.sells ?? "—"}</td>
              <td className="mono">{p.buyers ?? "—"} / {p.sellers ?? "—"}</td>
              <td className="mono">{ex.unknown ? <span className="muted" title={ex.unknown}>unknown</span> : ex.executable_return_pct !== undefined
                ? <><span className={cls(ex.executable_return_pct)}>{pct(ex.executable_return_pct)}</span> <span className="muted">({pct(ex.theoretical_return_pct)})</span></>
                : "—"}</td>
            </tr>);
        })}</tbody>
      </table>
    </div>
  );
}

export function RowAnalysis({ row }: { row: LedgerRow }) {
  const cf = row.analysis?.counterfactual; const rec = row.analysis?.recovery; const ex = row.post_exit; const lab = row.labels;
  const shadow = row.ml_shadow?.scores ?? {};
  return (
    <div className="stat-grid">
      {cf && <Stat label="Counterfactual" hint={cf.rule}>{cf.classification}
        <div className="muted small">rule exit {cf.rule_exit ?? "—"} · peak {pct(cf.peak_pct)} after {cf.time_to_peak_seconds ?? "—"}s ·
          drawdown before peak {pct(cf.drawdown_before_peak_pct)}{cf.better_later_entry ? ` · better entry later (${pct(cf.better_later_entry.pct_below_decision)})` : ""}
          {cf.rejecting_rule ? ` · rejected by: ${cf.rejecting_rule}` : ""}</div></Stat>}
      {ex && <Stat label="Exit review">{ex.classification}
        <div className="muted small">after exit: peak {pct(ex.post_exit_peak_pct)}, low {pct(ex.post_exit_min_pct)}
          {Object.entries(ex.horizons ?? {}).map(([k, v]: [string, any]) => ` · ${k} ${pct(v.change_pct)}`).join("")}</div></Stat>}
      {rec && !rec.unknown && <Stat label="Recovery path">{rec.recovered === null ? "no drawdown below the decision price" : rec.recovered ? "recovered" : "no recovery"}
        <div className="muted small">MAE {pct(rec.mae_pct)} at {rec.time_to_trough_seconds}s · MFE {pct(rec.mfe_pct)}
          {rec.time_to_recovery_seconds ? ` · back to entry after ${rec.time_to_recovery_seconds}s` : ""}
          · after the low: {rec.flow_after_trough_60s?.buyers ?? 0} buyers / {rec.flow_after_trough_60s?.sellers ?? 0} sellers</div></Stat>}
      {lab && <Stat label="Labels (known at T+60m)">{["upside_50", "upside_100", "migrate_60m", "fast_dump", "rug_60m", "recovery"]
        .filter((k) => lab[k]).join(", ") || "none"}</Stat>}
      {Object.keys(shadow).length > 0 && <Stat label="Shadow model scores" hint="review only — never used for decisions">
        <span className="small mono">{Object.entries(shadow).map(([k, v]: [string, any]) => `${k} ${Number(v.value).toFixed(2)}`).join(" · ")}</span></Stat>}
    </div>
  );
}

function Rows({ category, days }: { category: string; days: number }) {
  const [open, setOpen] = useState<string | null>(null);
  const q = useApi<{ total: number; total_capped?: boolean; items: LedgerRow[] }>("/api/ml/opportunities", { category, days, limit: 25 }, { refreshMs: 60000 });
  if (q.error) return <ErrorNotice error={q.error} />;
  if (!q.data) return <Loading />;
  if (q.data.items.length === 0) return <Empty>Nothing in this category yet.</Empty>;
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead><tr><th>Token</th><th>Stage / decision</th><th>Why</th><th>Path (change from the decision)</th><th>Executable @T+5m</th><th /></tr></thead>
        <tbody>{q.data.items.map((r) => (
          <Fragment key={r.id}>
            <tr>
              <td><TokenLink mint={r.mint} label={r.symbol} /><div className="muted">{formatDate(r.decided_at)}</div></td>
              <td>{r.stage} · {r.decision}{r.traded ? " · traded" : ""}</td>
              <td className="muted">{(r.reasons ?? []).slice(0, 2).join("; ") || "—"}</td>
              <td><span className="hz">{PATH_HORIZONS.map((h) => {
                const x = r.path?.[h] ?? r.horizons?.[h];
                return <span key={h} className={cls(x?.change_pct)}>{h.replace("T+", "")} {x?.change_pct !== undefined ? pct(x.change_pct) : "…"}</span>;
              })}</span></td>
              <td className={`mono ${cls(r.executable_return_pct)}`}>{r.executable_return_pct === null ? "unknown" : pct(r.executable_return_pct)}</td>
              <td><button className="btn btn-sm" onClick={() => setOpen(open === r.id ? null : r.id)} aria-expanded={open === r.id}>
                {open === r.id ? "Hide" : "Path"}</button></td>
            </tr>
            {open === r.id && <tr><td colSpan={6}><PathView row={r} /><RowAnalysis row={r} /></td></tr>}
          </Fragment>))}</tbody>
      </table>
      <p className="muted">{q.data.total}{q.data.total_capped ? "+" : ""} in this category over the last {days} days{q.data.total_capped ? " (exact count in the summary above)" : ""}.</p>
    </div>
  );
}

/** ML Review of the opportunity ledger: what the system saw, traded,
 * rejected, and what each decision would have been worth after costs. */
export default function LedgerReview() {
  const [days, setDays] = useState(7);
  const [cat, setCat] = useState("missed_win");
  const r = useApi<Review>("/api/ml/ledger-review", { days }, { refreshMs: 60000 });
  const d = r.data && reviewReady(r.data) ? r.data : null;
  const refresh = () => { void apiGet("/api/ml/ledger-review", { days, refresh: true }).catch(() => undefined).then(() => r.reload()); };
  return (
    <>
      <Section title="Ledger review — every decision and what it was worth"
        actions={<select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="Period">
          {[1, 7, 30].map((x) => <option key={x} value={x}>last {x} day{x > 1 ? "s" : ""}</option>)}</select>}>
        {r.error ? <ErrorNotice error={r.error} /> : r.data && !d ? <ReviewStatus data={r.data} onRefresh={refresh} /> : !d ? <Loading /> : (
          <>
            <div className="chip-row" role="tablist" aria-label="Categories">
              {CATS.map(([k, label, Icon]) => (
                <button key={k} role="tab" aria-selected={cat === k} className={`chip ${cat === k ? "chip-active" : ""}`} onClick={() => setCat(k)}>
                  <Icon size={14} aria-hidden /> {label} <b>{d.counts[k] ?? 0}</b>
                </button>))}
            </div>
            <Rows category={cat} days={days} />
            <p className="muted">{d.note}</p>
            <ReviewStatus data={d} onRefresh={refresh} />
          </>
        )}
      </Section>
      {d && (
        <Section title="Missed winners by bucket · signal vs execution · snipe latency">
          <div className="stat-grid">
            {Object.entries(d.missed_win_buckets).map(([k, b]) => (
              <Stat key={k} label={`Missed wins by ${k.replaceAll("_", " ")}`}>
                <span className="small">{Object.entries(b).sort((a, z) => z[1] - a[1]).map(([x, c]) => `${x}: ${c}`).join(" · ") || "none"}</span>
              </Stat>))}
            <Stat label={`Signal vs execution @${d.signal_vs_execution.horizon}`} hint={d.signal_vs_execution.note}>
              <span className={cls(d.signal_vs_execution.theoretical_return_avg_pct)}>{pct(d.signal_vs_execution.theoretical_return_avg_pct)}</span>
              {" chart → "}<span className={cls(d.signal_vs_execution.executable_return_avg_pct)}>{pct(d.signal_vs_execution.executable_return_avg_pct)}</span>
              {" executable"}<div className="muted small">{d.signal_vs_execution.rows} rows · reference {d.signal_vs_execution.reference_size_sol} SOL</div>
            </Stat>
            {Object.entries(d.snipe_latency).map(([k, q]) => (
              <Stat key={k} label={k.replaceAll("_", " ")}>{q.median ?? "—"} <span className="muted small">median · p90 {q.p90 ?? "—"} · n {q.n}</span></Stat>))}
          </div>
        </Section>
      )}
      {d && (
        <Section title="Shadow models (multi-target) — review only">
          <p className="muted"><FlaskConical size={14} aria-hidden /> Trained on the ledger with a time-based split; scored onto recent
            decisions for review. Never loaded by the decision engine, never promotable, never used for sizing.
            P_MANIPULATION is not trained (no ground-truth label).</p>
          {d.shadow_models.length === 0 ? <Empty>No shadow model yet: training starts at 200 completed, labelled opportunities.</Empty> : (
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr><th>Target</th><th>Version</th><th>ROC-AUC</th><th>PR-AUC</th><th>Brier</th><th>Precision@10</th>
                  <th>Holdout n / positives</th><th>MAE vs baseline</th><th>Split</th></tr></thead>
                <tbody>{d.shadow_models.map((m) => {
                  const h = m.holdout ?? {};
                  return (
                    <tr key={m.name}>
                      <td>{m.target} <span className="pill">SHADOW</span></td><td>v{m.version}</td>
                      <td className="mono">{h.roc_auc ?? "—"}</td><td className="mono">{h.pr_auc ?? "—"}</td>
                      <td className="mono">{h.brier ?? "—"}</td><td className="mono">{h.precision_at_10 ?? "—"}</td>
                      <td className="mono">{h.n ?? "—"} / {h.positives ?? "—"}</td>
                      <td className="mono">{h.mae !== undefined ? `${h.mae} vs ${h.baseline_mae_train_mean}` : "—"}</td>
                      <td className="muted small">train {m.split?.train} · holdout from {m.split?.holdout_start ? formatDate(m.split.holdout_start) : "—"} · purged {m.split?.purged}</td>
                    </tr>);
                })}</tbody>
              </table>
            </div>
          )}
        </Section>
      )}
    </>
  );
}
