"use client";

import Link from "next/link";
import { useState } from "react";
import { Activity, Brain, FlaskConical, History, LineChart, ShieldQuestion } from "lucide-react";
import { Empty, ErrorNotice, Loading, ReviewStatus, Section, Stat, TokenLink, reviewReady } from "@/components/ui";
import { apiGet } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const v = (x: unknown) => (x === null || x === undefined ? "—" : String(x));
const STATUS_CLASS = (s: string) =>
  s.startsWith("CHALLENGER OUTPERFORMING") || s === "CHAMPION IN USE" ? "pill pill-ok"
    : s.startsWith("AWAITING") ? "pill pill-warn"
      : s === "VALIDATION FAILED" || s === "IMPROVEMENT NOT CONFIRMED" ? "pill pill-danger" : "pill pill-off";

function Pill({ s }: { s: string }) {
  return <span className={STATUS_CLASS(s)}>{s}</span>;
}

/** ML engine monitoring (GET /api/ml/monitor, yonixalpha_core.ml_monitor):
 * saved records only. Predictions and executed paper positions are shown apart. */
export function MlMonitorSummary() {
  const q = useApi<J>("/api/ml/monitor", { days: 7 }, { refreshMs: 120000 });
  const d = q.data && reviewReady(q.data) ? q.data : null;
  const refresh = () => { void apiGet("/api/ml/monitor", { days: 7, refresh: true }).catch(() => undefined).then(() => q.reload()); };
  return (
    <Section title="ML engine monitoring (last 7 days)">
      <ErrorNotice error={q.error} />
      {q.loading && !q.data && <Loading />}
      {q.data && !d && <ReviewStatus data={q.data} onRefresh={refresh} />}
      {d && (
        <>
          <h3 className="small"><Brain size={14} aria-hidden /> Models: champion and challenger</h3>
          {d.models.length === 0 ? <Empty>No trained model saved yet.</Empty> : (
            <div className="table-scroll"><table className="data-table">
              <thead><tr><th>Model</th><th>Champion</th><th>Challenger</th><th>Shadow</th><th>Trained</th><th>Samples</th>
                <th>Stage / contribution</th><th>Status</th><th>Why</th></tr></thead>
              <tbody>{d.models.map((m: J) => (
                <tr key={m.name}><td>{m.name}</td><td>{v(m.champion)}</td><td>{v(m.challenger)}</td><td>{v(m.shadow)}</td>
                  <td>{m.trained_at ? formatDate(m.trained_at) : "—"}</td><td>{v(m.training_samples)}</td>
                  <td>{m.stage} · {m.contribution_pct} %</td><td><Pill s={m.status} /></td><td className="small">{m.why}</td></tr>))}</tbody>
            </table></div>)}
          <p className="muted small">A challenger becomes champion only when you promote it on <Link className="link" href="/dashboard/ml/review">ML Review</Link>.</p>

          <h3 className="small"><LineChart size={14} aria-hidden /> Predictions (recorded before their outcome)</h3>
          <div className="stat-grid">
            <Stat label="Experiment signals: total / evaluated / pending">{d.signals.total} / {d.signals.evaluated} / {d.signals.pending}</Stat>
            <Stat label="Signals in the window">{d.signals.in_window}</Stat>
            <Stat label="Shadow scores in the window" hint="PROSPECTIVE: made before the outcome; RETROSPECTIVE: counted, never evaluated">
              {d.shadow_predictions.rows_scored} rows · {Object.values(d.shadow_predictions.models as J).reduce((a: number, m: any) => a + m.PROSPECTIVE, 0)} prospective
            </Stat>
            <Stat label="Return model, prospective: favoured vs not (mean exec. return)">
              {v(d.shadow_predictions.return_model_prospective.favoured.mean_executable_return_pct)} % (n={d.shadow_predictions.return_model_prospective.favoured.n}) vs {v(d.shadow_predictions.return_model_prospective.not_favoured.mean_executable_return_pct)} % (n={d.shadow_predictions.return_model_prospective.not_favoured.n})
            </Stat>
          </div>
          <p className="muted small">{d.shadow_predictions.return_model_prospective.note}</p>

          <h3 className="small"><Activity size={14} aria-hidden /> Paper trades executed (not predictions)</h3>
          <div className="table-scroll"><table className="data-table">
            <thead><tr><th>Strategy</th><th>Closed</th><th>W / L / BE</th><th>Win %</th><th>Avg win / loss (SOL)</th><th>Profit factor</th>
              <th>Net (SOL)</th><th>Net per trade</th><th>Max drawdown</th><th>Open</th><th>Forward test</th></tr></thead>
            <tbody>{Object.entries(d.paper_trades as J).map(([e, t]: [string, any]) => {
              const c = t.closed;
              const fwd = d.forward_test.strategies[`${e} PAPER`];
              return (
                <tr key={e}><td>{e}</td><td>{c ? c.trades : 0}</td><td>{c ? `${c.wins} / ${c.losses} / ${c.breakeven}` : "—"}</td>
                  <td>{c ? v(c.win_rate_pct) : "—"}</td><td>{c ? `${v(c.avg_win_sol)} / ${v(c.avg_loss_sol)}` : "—"}</td>
                  <td>{c ? v(c.profit_factor) : "—"}</td><td className={c && c.net_pnl_sol < 0 ? "neg" : c && c.net_pnl_sol > 0 ? "pos" : ""}>{c ? v(c.net_pnl_sol) : "—"}</td>
                  <td>{c ? v(c.expectancy_sol) : "—"}</td><td>{c ? v(c.max_drawdown_sol) : "—"}</td>
                  <td>{t.open.reduce((a: number, o: J) => a + o.count, 0)}</td>
                  <td>{fwd ? <><Pill s={fwd.status} /> <span className="muted small">{fwd.gaps.join("; ")}</span></> : <span className="muted small">no forward test</span>}</td></tr>);
            })}</tbody>
          </table></div>
          <p className="muted small">Rejected or watched decisions in the window: {d.rejected.decisions}; outcome measured for {d.rejected.measured};
            positive afterwards {d.rejected.positive_after}. See the event history below and <Link className="link" href="/dashboard/performance">Solana Performance</Link>.</p>

          <h3 className="small"><FlaskConical size={14} aria-hidden /> Experiments</h3>
          <p className="small">Proposed {d.experiment_counts.DRAFT ?? 0} · accepted {d.experiment_counts.ACCEPTED ?? 0} · rejected {d.experiment_counts.REJECTED ?? 0} · retired {d.experiment_counts.RETIRED ?? 0}</p>
          {d.experiments.length === 0 ? <Empty>No experiment yet: write one in the <Link className="link" href="/dashboard/ml/lab">ML Training Lab</Link>.</Empty> : (
            <ul className="small">{d.experiments.slice(0, 10).map((e: J) => (
              <li key={e.id}><Pill s={e.status} /> {e.idea} <span className="muted">— {e.why}</span></li>))}</ul>)}

          <h3 className="small"><ShieldQuestion size={14} aria-hidden /> Activity, data and audit trail</h3>
          <div className="stat-grid">
            <Stat label="Data sources">{v(d.data_sources?.overall)}</Stat>
            {Object.entries((d.activity ?? {}) as J).slice(0, 6).map(([k, s]: [string, any]) => (
              <Stat key={k} label={`ML step ${k}`} hint={s.error ?? s.reason ?? undefined}>{s.state ?? "—"}{s.last_ok_at ? ` · last success ${formatDate(s.last_ok_at)}` : " · never succeeded"}</Stat>))}
          </div>
          <p className="small">Audit: {Object.entries(d.audit.counts as J).map(([k, n]) => `${k} ${n}`).join(" · ") || "no ML change recorded"}</p>
          <ul className="muted small">{(d.notes as string[]).map((n) => <li key={n}>{n}</li>)}</ul>
          <ReviewStatus data={d} onRefresh={refresh} />
        </>
      )}
    </Section>
  );
}

/** Signal -> decision -> paper entry -> exit -> evaluation (GET /api/ml/history). */
export function MlHistory() {
  const [traded, setTraded] = useState<string>("");
  const q = useApi<{ rows: J[] }>("/api/ml/history", { hours: 48, limit: 100, traded: traded === "" ? undefined : traded },
    { refreshMs: 60000 });
  return (
    <Section title="Event history" actions={
      <select aria-label="Filter" value={traded} onChange={(e) => setTraded(e.target.value)}>
        <option value="">all decisions</option><option value="true">traded</option><option value="false">not traded</option>
      </select>}>
      <p className="muted small"><History size={12} aria-hidden /> Newest first, last 48 h. Open a row for its full timeline in the ML Training Lab.</p>
      <ErrorNotice error={q.error} />
      {q.loading && !q.data && <Loading />}
      {q.data && q.data.rows.length === 0 && <Empty>No decision recorded in the last 48 hours.</Empty>}
      {q.data && q.data.rows.length > 0 && (
        <div className="table-scroll"><table className="data-table">
          <thead><tr><th>Decided</th><th>Token</th><th>Strategy</th><th>Steps</th><th /></tr></thead>
          <tbody>{q.data.rows.map((r) => (
            <tr key={r.id}><td>{formatDate(r.decided_at)}</td><td><TokenLink mint={r.mint} label={r.symbol} /></td><td>{r.engine}</td>
              <td className="small">{r.steps.map((s: J, i: number) => <div key={i}><b>{s.step}</b>{s.at ? ` ${formatDate(s.at)}` : ""}: {s.text}</div>)}</td>
              <td><Link className="btn btn-sm" href={`/dashboard/ml/lab?observation=${r.id}`}>Open</Link></td></tr>))}</tbody>
        </table></div>)}
    </Section>
  );
}
