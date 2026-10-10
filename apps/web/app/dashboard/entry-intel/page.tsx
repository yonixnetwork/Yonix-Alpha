"use client";

import { useEffect, useState } from "react";
import { Timer } from "lucide-react";
import { Empty, ErrorNotice, Loading, PageHeader, Section, Stat, TokenLink } from "@/components/ui";
import { apiPut } from "@/lib/api";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const MODES = ["SHADOW", "PAPER", "PAUSED"];
const v = (x: any, d = 2) => (x === null || x === undefined ? "—" : typeof x === "number" ? (Number.isInteger(x) ? String(x) : x.toFixed(d)) : String(x));
const pct = (x: any) => (x === null || x === undefined ? "—" : `${Number(x).toFixed(1)}%`);
const rate = (x: any) => (x === null || x === undefined ? "—" : `${(Number(x) * 100).toFixed(1)}%`);
const decisionClass = (d: string) => (d === "CANDIDATE" ? "pos" : d === "NO_TRADE" ? "neg" : "");
const READY_CLASS: Record<string, string> = {
  PRODUCTION_CONTRIBUTOR: "pill pill-ok", PAPER_VALIDATED: "pill pill-ok", SHADOW: "pill pill-off",
  VALIDATING: "pill pill-warn", LEARNING: "pill pill-off", INSUFFICIENT_DATA: "pill pill-off",
  DRIFT_DETECTED: "pill pill-danger", PAUSED: "pill pill-off", BASELINE: "pill pill-off",
};

function ActiveTokens({ rows }: { rows: J[] }) {
  if (!rows.length) return <Empty>No token evaluated in the last hour. The shadow pass writes a state for each fresh pump.fun token with new trades.</Empty>;
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead><tr>
          <th>Token</th><th>Age (s)</th><th>Phase</th><th>Mcap (SOL)</th><th>Curve</th><th>Inflow SOL/s</th><th>Accel.</th>
          <th>Trades/s</th><th>Buyers +10s</th><th>Sellers +10s</th><th>Move since launch</th><th>From peak</th>
          <th>Round trip cost</th><th>Smart wallets</th><th>ML (shadow)</th><th>Strategies</th>
        </tr></thead>
        <tbody>{rows.map((s) => (
          <tr key={s.mint}>
            <td><TokenLink mint={s.mint} label={s.symbol} />{s.complete_history === false && <span className="muted small" title="the stream started after this token's launch: its early trades are missing"> (partial history)</span>}</td>
            <td>{v(s.age_seconds, 0)}</td>
            <td title={(s.phase_evidence ?? []).join("; ")}>{s.phase}</td>
            <td>{v(s.market_cap_sol)}</td>
            <td>{s.curve_progress === null || s.curve_progress === undefined ? "—" : `${(s.curve_progress * 100).toFixed(1)}%`}</td>
            <td>{v(s.inflow_sol_per_s, 3)}</td>
            <td>{v(s.inflow_acceleration, 3)}</td>
            <td>{v(s.trade_rate_per_s)}</td>
            <td>{v(s.buyer_growth_10s, 0)}</td>
            <td>{v(s.seller_growth_10s, 0)}</td>
            <td>{pct(s.displacement_pct)}</td>
            <td>{pct(s.drawdown_from_peak_pct)}</td>
            <td>{pct(s.round_trip_cost_pct)}</td>
            <td title={s.smart_wallets?.reason ?? ""}>{s.smart_wallets ? `${s.smart_wallets.status ?? "—"} (${s.smart_wallets.proven_wallets ?? 0} proven)` : "—"}</td>
            <td>{s.ml_probability === null || s.ml_probability === undefined ? "—" : Number(s.ml_probability).toFixed(3)}</td>
            <td>{Object.entries(s.strategies ?? {}).map(([name, d]: [string, any]) => (
              <div key={name} className="small" title={[...(d.reasons ?? []), ...(d.positives ?? [])].join("; ")}>
                {name}: <span className={decisionClass(d.decision)}>{d.decision}</span>
                {d.reasons?.length ? <span className="muted"> — {d.reasons[0]}</span> : null}
              </div>))}</td>
          </tr>))}</tbody>
      </table>
    </div>
  );
}

function SettingsEditor({ ov, onSaved }: { ov: J; onSaved: () => void }) {
  const [modes, setModes] = useState<Record<string, string>>(ov.modes ?? {});
  const [interval, setInterval_] = useState<string>(String(ov.config?.event_min_interval_seconds ?? 10));
  const [eventsOn, setEventsOn] = useState<boolean>(!!ov.config?.event_reevaluation);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setModes(ov.modes ?? {}); }, [ov.modes]);
  const editable = [...(ov.strategies ?? []), ...(ov.migrated_variants ?? [])];
  const save = async () => {
    setBusy(true); setMsg(null);
    try {
      await apiPut("/api/entry-intel/settings", {
        modes, config: { event_reevaluation: eventsOn, event_min_interval_seconds: Number(interval) },
      });
      setMsg("Saved. The shadow pass reads it within 30 seconds."); onSaved();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  };
  return (
    <div>
      <p className="muted small">
        SHADOW records signals only. PAPER also sends the token to the safety gate as a PAPER-only candidate (every safety,
        risk and sellability check still applies; it can never become a LIVE order). LIVE use is not available in this release.
        SMART_WALLET_CONFIRMATION never trades by itself and migrated variants are measured in SHADOW only.
      </p>
      <div className="table-scroll">
        <table className="data-table">
          <thead><tr><th>Strategy</th><th>Mode</th></tr></thead>
          <tbody>{editable.map((name: string) => {
            const paperAllowed = name !== "SMART_WALLET_CONFIRMATION" && !(ov.migrated_variants ?? []).includes(name);
            return (
              <tr key={name}><td>{name}</td><td>
                <select aria-label={`Mode of ${name}`} value={modes[name] ?? "SHADOW"} onChange={(e) => setModes({ ...modes, [name]: e.target.value })}>
                  {MODES.filter((m) => m !== "PAPER" || paperAllowed).map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
              </td></tr>);
          })}</tbody>
        </table>
      </div>
      <div className="form-row">
        <label><input type="checkbox" checked={eventsOn} onChange={(e) => setEventsOn(e.target.checked)} /> Re-evaluate gate candidates early on a meaningful market event</label>
      </div>
      <div className="form-row">
        <label>Minimum seconds between event re-evaluations (3-300)
          <input type="number" min={3} max={300} value={interval} onChange={(e) => setInterval_(e.target.value)} />
        </label>
      </div>
      <div className="btn-row"><button className="btn btn-sm" disabled={busy} onClick={save}>{busy ? "Saving…" : "Save"}</button></div>
      {msg && <p className="small">{msg}</p>}
    </div>
  );
}

function Evaluation({ ev }: { ev: J }) {
  const names = Object.keys(ev.strategies ?? {});
  return (
    <div>
      <p className="muted small">
        Returns are percent of a reference-size trade after fees, price impact, measured entry latency and fixed costs.
        Champion: {ev.champion} (the existing pipeline). Test period: {ev.frozen ? `${ev.frozen.test_start} to ${ev.frozen.test_end} (frozen)` : "not frozen yet"}.
        {" "}{ev.note}
      </p>
      <div className="table-scroll">
        <table className="data-table">
          <thead><tr><th>Strategy / baseline</th><th>Readiness</th><th>Signals</th><th>With return</th><th>Win rate</th>
            <th>Median %</th><th>Profit factor</th><th>Bad entries</th><th>Late entries</th><th>Max drawdown (pts)</th><th>Test median %</th></tr></thead>
          <tbody>{names.map((n) => {
            const s = ev.strategies[n]; const a = s.all ?? {}; const t = s.by_split?.test ?? {};
            return (
              <tr key={n}>
                <td>{n}</td>
                <td><span className={READY_CLASS[s.readiness?.state] ?? "pill pill-off"} title={s.readiness?.reason}>{s.readiness?.state}</span>
                  <div className="muted small">{s.readiness?.reason}</div></td>
                <td>{v(a.signals)}</td><td>{v(a.with_executable_return)}</td><td>{rate(a.win_rate)}</td>
                <td>{v(a.median_return_pct)}</td><td>{v(a.profit_factor)}</td><td>{rate(a.bad_entry_rate)}</td>
                <td>{rate(a.late_entry_rate)}</td><td>{v(a.max_drawdown_pct_points)}</td><td>{v(t.median_return_pct)}</td>
              </tr>);
          })}</tbody>
        </table>
      </div>
      {ev.labelled === 0 && <Empty>No labelled signal yet. Signals are labelled 15 minutes after they are recorded.</Empty>}
    </div>
  );
}

function Latency({ lat }: { lat: J }) {
  const stages = Object.entries(lat.latency_seconds ?? {}) as [string, J][];
  const waits = Object.entries(lat.waiting_seconds_median ?? {}) as [string, any][];
  const codes = Object.entries(lat.top_blocking_codes ?? {}) as [string, any][];
  return (
    <div>
      <div className="stat-grid">
        <Stat label="Candidates">{lat.candidates}</Stat>
        <Stat label="Entered">{lat.entered}</Stat>
      </div>
      <div className="table-scroll">
        <table className="data-table">
          <thead><tr><th>Stage</th><th>Median (s)</th><th>p90 (s)</th><th>Samples</th></tr></thead>
          <tbody>{stages.map(([k, s]) => <tr key={k}><td>{k}</td><td>{v(s.median)}</td><td>{v(s.p90)}</td><td>{s.n}</td></tr>)}</tbody>
        </table>
      </div>
      {!stages.some(([, s]) => s.n) && <Empty>No timed stage in this window.</Empty>}
      <h3 className="small">Why candidates waited before approval</h3>
      <div className="stat-grid">
        {waits.map(([k, x]) => <Stat key={k} label={`${k} (median s; total ${v(lat.waiting_seconds_total?.[k], 0)} s)`}>{v(x)}</Stat>)}
      </div>
      {codes.length > 0 && (
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Most frequent blocking reason</th><th>Evaluations</th></tr></thead>
            <tbody>{codes.map(([c, n]) => <tr key={c}><td>{c}</td><td>{n}</td></tr>)}</tbody>
          </table>
        </div>)}
    </div>
  );
}

function LateEntries({ late }: { late: J }) {
  const rows = (late.rows ?? []) as J[];
  return (
    <div>
      <div className="stat-grid">
        <Stat label="Entries">{late.entries}</Stat>
        <Stat label="Entered 50%+ above the first detection price">{late.late_by_50pct_or_more}</Stat>
      </div>
      {rows.length === 0 ? <Empty>No entry in this window.</Empty> : (
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Token</th><th>Mode</th><th>Detection to entry (s)</th><th>Price change since detection</th>
              <th>First strategy signal</th><th>Signal to entry (s)</th><th>Price change since signal</th><th>Decelerating at entry</th><th>Result</th></tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.position_id}>
                <td><TokenLink mint={r.mint} label={r.symbol} /></td><td>{r.mode}</td>
                <td>{v(r.seconds_detection_to_entry, 1)}</td>
                <td className={(r.price_change_detection_to_entry_pct ?? 0) >= 50 ? "neg" : ""} title={r.unknown ?? r.prices_from ?? ""}>{pct(r.price_change_detection_to_entry_pct)}</td>
                <td>{r.first_signal_strategy ?? "—"}</td><td>{v(r.seconds_signal_to_entry, 1)}</td><td>{pct(r.price_change_signal_to_entry_pct)}</td>
                <td title={(r.deceleration_evidence ?? []).join("; ")}>{r.decelerating_at_entry ? "yes" : "no"}</td>
                <td className={r.realized_pnl_pct > 0 ? "pos" : r.realized_pnl_pct < 0 ? "neg" : ""}>{pct(r.realized_pnl_pct)}</td>
              </tr>))}</tbody>
          </table>
        </div>)}
    </div>
  );
}

function Parity({ par }: { par: J }) {
  const rows = (par.rows ?? []) as J[];
  const agg = (m: string, a: J) => (
    <Stat key={m} label={`${m}: entries / closed / win rate / median PnL / median fees % of size`}>
      {a.entries} / {a.closed} / {rate(a.win_rate)} / {pct(a.median_pnl_pct)} / {pct(a.median_fees_pct_of_size)}
    </Stat>);
  return (
    <div>
      <p className="muted small">{par.note}. Entry latency used for estimates: {v(par.latency_seconds, 1)} s ({par.latency_source}).</p>
      <div className="stat-grid">
        {agg("PAPER", par.paper ?? {})}{agg("LIVE", par.live ?? {})}
        <Stat label="PAPER entries: estimated live price move during the latency (median)">{pct(par.paper_estimated_live_displacement_median_pct)}</Stat>
        <Stat label="LIVE entries refused after gate approval">{par.live_refused_after_gate_approval?.count ?? 0}</Stat>
      </div>
      {Object.keys(par.live_refused_after_gate_approval?.by_reason ?? {}).length > 0 && (
        <p className="small">Refusal reasons: {Object.entries(par.live_refused_after_gate_approval.by_reason).map(([k, n]) => `${k} (${n})`).join(", ")}</p>)}
      {rows.length === 0 ? <Empty>No entry in this window.</Empty> : (
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Token</th><th>Mode</th><th>Entry vs decision price</th><th>Fees % of size</th><th>Size</th><th>Result</th><th>Why paper and live differ</th></tr></thead>
            <tbody>{rows.slice(0, 50).map((r) => (
              <tr key={r.position_id}>
                <td><TokenLink mint={r.mint} label={r.symbol} /></td><td>{r.mode}</td>
                <td>{pct(r.entry_displacement_pct)}{r.est_live_displacement_pct !== undefined && <div className="muted small">est. live {pct(r.est_live_displacement_pct)}</div>}</td>
                <td>{pct(r.fees_pct_of_size)}</td><td>{v(r.size_quote, 4)}</td>
                <td className={r.realized_pnl_pct > 0 ? "pos" : r.realized_pnl_pct < 0 ? "neg" : ""}>{pct(r.realized_pnl_pct)}</td>
                <td className="small">{(r.difference_reasons ?? []).join("; ") || "—"}</td>
              </tr>))}</tbody>
          </table>
        </div>)}
    </div>
  );
}

/** Early-entry intelligence (apps/api /entry-intel): the entry strategies run
 * in SHADOW next to the existing pipeline; nothing on this page can place an
 * order. */
export default function EntryIntelPage() {
  const ov = useApi<J>("/api/entry-intel/overview", undefined, { refreshMs: 10000 });
  const ev = useApi<J>("/api/entry-intel/evaluation", { days: 30 }, { refreshMs: 120000 });
  const lat = useApi<J>("/api/entry-intel/latency", { hours: 6 }, { refreshMs: 60000 });
  const late = useApi<J>("/api/entry-intel/late-entries", { days: 2 }, { refreshMs: 120000 });
  const par = useApi<J>("/api/entry-intel/parity", { days: 7 }, { refreshMs: 120000 });
  const o = ov.data;
  return (
    <div>
      <PageHeader title="Entry Intelligence" icon={<Timer size={20} aria-hidden />}
        subtitle="Early-acceleration, smart-wallet confirmation and momentum-continuation strategies measured in SHADOW against the existing pipeline: when tokens are seen, when a signal appears, when we enter and what that timing costs. No result here is a promise of future profit." />
      <ErrorNotice error={ov.error} />
      {ov.loading && !o && <Loading />}
      {o && (
        <>
          <Section title="Status">
            <div className="stat-grid">
              <Stat label="Mode">{o.mode}</Stat>
              <Stat label="Shadow pass">{o.pass?.paused_at && !o.pass?.last_pass_at ? "paused (host resources critical)" : o.pass?.last_pass_at
                ? `last pass ${o.pass.last_pass_at}: ${o.pass.considered ?? 0} tokens, ${o.pass.evaluated ?? 0} evaluated, ${o.pass.recorded ?? 0} recorded` : "not running yet"}</Stat>
              <Stat label="Entry latency used by the labeller">{v(o.label_latency?.seconds, 1)} s ({o.label_latency?.source})</Stat>
              <Stat label="Entry-timing model">{o.model ? `v${o.model.version} ${o.model.status}` : "not trained (needs labelled signals)"}</Stat>
            </div>
            {o.model && <p className="muted small">{o.model.contribution}</p>}
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr><th>Strategy / baseline (24 h)</th><th>Signals</th><th>Labelled</th></tr></thead>
                <tbody>{Object.entries(o.counts ?? {}).map(([k, c]: [string, any]) => <tr key={k}><td>{k}</td><td>{c.signals}</td><td>{c.labelled}</td></tr>)}</tbody>
              </table>
            </div>
          </Section>
          <Section title="Tokens being evaluated now"><ActiveTokens rows={o.active ?? []} /></Section>
          <Section title="Strategy modes and event re-evaluation"><SettingsEditor ov={o} onSaved={ov.reload} /></Section>
        </>
      )}
      <Section title="Strategy comparison and readiness (chronological, frozen test period)">
        <ErrorNotice error={ev.error} />{ev.data ? <Evaluation ev={ev.data} /> : ev.loading && <Loading />}
      </Section>
      <Section title="Entry latency, last 6 hours">
        <ErrorNotice error={lat.error} />{lat.data ? <Latency lat={lat.data} /> : lat.loading && <Loading />}
      </Section>
      <Section title="Late entries, last 2 days">
        <ErrorNotice error={late.error} />{late.data ? <LateEntries late={late.data} /> : late.loading && <Loading />}
      </Section>
      <Section title="Paper vs LIVE parity per entry, last 7 days">
        <ErrorNotice error={par.error} />{par.data ? <Parity par={par.data} /> : par.loading && <Loading />}
      </Section>
    </div>
  );
}
