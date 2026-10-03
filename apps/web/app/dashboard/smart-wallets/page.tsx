"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight, Eye, Fingerprint } from "lucide-react";
import { ExternalBadges, ExternalDetail, ExternalPanel } from "@/components/ExternalIntel";
import { Empty, ErrorNotice, Loading, PageHeader, Section } from "@/components/ui";
import { apiPost, apiPut } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const CHAINS: [string, string][] = [["", "All chains"], ["solana", "Solana"], ["bsc", "BSC"], ["robinhood", "Robinhood Chain"]];
const SORTS: [string, string][] = [["last_seen", "Last seen"], ["trades", "Trades"], ["tokens", "Tokens"], ["score", "Score"]];
const LABELS = ["", "SNIPER", "SCALPER", "HOLDER", "HIGH_ACTIVITY", "POSSIBLE_BOT", "DELEGATED_WALLET", "CONTRACT"];
const STAGES: [string, string][] = [["", "Any"], ["COLLECTING_HISTORY", "Collecting history"], ["VALIDATED", "Validated"],
  ["PAPER_FOLLOWED", "Validated + paper-followed"], ["REJECTED", "Rejected"]];
const STAGE_CLASS: Record<string, string> = { PAPER_FOLLOWED: "pill pill-ok", VALIDATED: "pill pill-ok", REJECTED: "pill pill-danger", COLLECTING_HISTORY: "pill pill-off" };
const CHECK_LABEL: Record<string, string> = {
  min_trades: "Trades", min_closed: "Closed trades", min_active_days: "Active days", min_active_weeks: "Active weeks",
  min_unique_tokens: "Unique tokens", min_history_days: "History coverage (days)", min_profitable_periods: "Profitable days",
  min_profitable_period_share: "Share of active days profitable", max_drawdown_pct: "Max drawdown (% of capital in)",
  min_profit_factor: "Profit factor", max_single_trade_share: "Best trade share of gains", min_median_return_pct: "Median return %",
};

function Validation({ v, r, pf }: { v: J | undefined; r: J | undefined; pf: J | undefined }) {
  if (!v) return null;
  return (
    <div style={{ marginTop: 10 }}>
      <h4 className="small">Validation: <span className={v.status === "VALIDATED" ? "pill pill-ok" : v.status === "NOT_VALIDATED" ? "pill pill-danger" : "pill pill-off"}>{v.status === "INSUFFICIENT_DATA" ? "INSUFFICIENT DATA" : v.status.replace("_", " ")}</span> <span className="muted">{v.reason}</span></h4>
      {(v.checks ?? []).length > 0 && (
        <div className="table-scroll"><table className="data-table">
          <thead><tr><th>Check</th><th>Value</th><th>Required</th><th>Result</th></tr></thead>
          <tbody>{v.checks.map((c: J) => (
            <tr key={c.check}><td>{CHECK_LABEL[c.check] ?? c.check}</td><td>{c.value ?? "—"}{c.unit && c.value !== null ? ` ${c.unit}` : ""}</td>
              <td className="muted">{c.check.startsWith("max_") ? "≤ " : "≥ "}{c.required}</td>
              <td>{c.pass === null ? <span className="muted small">not measurable yet</span> : c.pass ? <span className="pos">pass</span> : <span className="neg">fail</span>}</td></tr>))}
          </tbody></table></div>)}
      {r && (
        <>
          <h4 className="small">Market regime test: <span className={r.status === "CONSISTENT" ? "pill pill-ok" : r.status === "REGIME_DEPENDENT" ? "pill pill-warn" : "pill pill-off"}>{r.status.replace("_", " ")}</span> <span className="muted">{r.reason}</span></h4>
          {Object.keys(r.dimensions ?? {}).length > 0 && (
            <div className="table-scroll"><table className="data-table">
              <thead><tr><th>Regime</th><th>Closed trades</th><th>Wins</th><th>Net PnL</th><th>Median return</th></tr></thead>
              <tbody>{Object.values(r.dimensions as Record<string, J>).flatMap((d: J) => Object.entries(d.sides as Record<string, J>).map(([side, st]) => (
                <tr key={side}><td>{side.replace("_", " ")}</td><td>{st.trades}</td><td>{st.wins}</td>
                  <td className={signed(st.net_pnl)}>{st.net_pnl === null ? <span className="muted small">no trades</span> : num(st.net_pnl)}</td>
                  <td>{st.median_return_pct === null ? "—" : `${st.median_return_pct}%`}{st.status !== "OK" && <span className="muted small"> (too few)</span>}</td></tr>)))}
              </tbody></table></div>)}
          {r.basis && <p className="muted small">{r.basis}</p>}
        </>)}
      {pf && (
        <p className="small">Paper follow (last {pf.buys_replayed} buys): <span className="pos">{pf.won} would have won</span> · <span className="neg">{pf.lost} would have lost</span>
          {pf.no_price_data ? ` · ${pf.no_price_data} no price data` : ""} · median {pf.median_result_pct === null ? "—" : `${pf.median_result_pct}%`}
          <span className="muted"> — {pf.basis}; entry {pf.assumed_detection_s}s after their buy, exit at their sell or {pf.horizon_min} min.</span></p>)}
    </div>
  );
}

function ValidationSettings() {
  const { data, error, reload } = useApi<J>("/api/wallets/validation-settings");
  const [draft, setDraft] = useState<J | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  if (error) return <ErrorNotice error={error} />;
  if (!data) return null;
  const vals = draft ?? data.settings;
  const save = async () => {
    try { await apiPut("/api/wallets/validation-settings", vals); setMsg("Saved. Applied on the next profile rebuild (every 10 minutes)."); setDraft(null); reload(); }
    catch (e) { setMsg(String((e as Error).message)); }
  };
  return (
    <Section title="Wallet validation rules">
      <p className="muted small">{data.note}. Trade-count and history checks decide INSUFFICIENT DATA; the others decide VALIDATED or NOT VALIDATED.</p>
      <div className="form-grid">
        {Object.keys(data.settings).map((k) => (
          <label key={k} className="small">{CHECK_LABEL[k] ?? k} ({k.startsWith("max_") ? "maximum" : "minimum"})
            <input value={vals[k]} inputMode="decimal" onChange={(e) => setDraft({ ...vals, [k]: e.target.value })} /></label>))}
      </div>
      <div className="btn-row"><button className="btn btn-sm" disabled={!draft} onClick={save}>Save rules</button>
        <button className="btn btn-ghost btn-sm" onClick={() => setDraft({ ...data.defaults })}>Reset to defaults</button></div>
      {msg && <p className="small">{msg}</p>}
    </Section>
  );
}
const pct = (v: any) => (v === null || v === undefined ? "—" : `${(Number(v) * 100).toFixed(0)}%`);
const NATIVE: Record<string, string> = { solana: "SOL", bsc: "BNB", robinhood: "ETH" };
const num = (v: any, d = 4) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));
const signed = (v: any) => (v === null || v === undefined ? "" : Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : "");
const hold = (s: any) => (s === null || s === undefined ? "—" : s < 120 ? `${Math.round(s)} s` : s < 7200 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);

const BEHAVIOUR_SHORT: Record<string, string> = {
  SUCCESSFUL_ENTRY_PATTERN: "success", FAILED_ENTRY_PATTERN: "failed", LATE_ENTRY: "late entry",
  PREMATURE_EXIT: "premature exit", LATE_EXIT: "late exit", MISSED_WINNER: "missed winner",
};

function BehaviourSummary({ b }: { b: J | undefined }) {
  if (!b) return <span className="muted small">—</span>;
  const l = (b.labels ?? {}) as Record<string, number>;
  return (
    <span className="small" title={Object.entries(l).map(([k, v]) => `${BEHAVIOUR_SHORT[k] ?? k}: ${v}`).join(" · ")}>
      {b.episodes} entries: <span className="pos">{l.SUCCESSFUL_ENTRY_PATTERN ?? 0}</span> / <span className="neg">{l.FAILED_ENTRY_PATTERN ?? 0}</span>
      {l.MISSED_WINNER ? <span className="muted"> · {l.MISSED_WINNER} missed</span> : null}
    </span>
  );
}

function BehaviourDetail({ chain, wallet }: { chain: string; wallet: string }) {
  const { data, error, loading } = useApi<J>(`/api/wallets/behaviour/${chain}/${wallet}`);
  if (error) return <ErrorNotice error={error} />;
  if (loading && !data) return <Loading />;
  if (!data) return null;
  const eps = (data.episodes ?? []) as J[];
  return (
    <div style={{ marginTop: 10 }}>
      <h4 className="small">Entry behaviour (labelled a day after each launch, last 14 days)</h4>
      <p className="muted small">{data.note}</p>
      {eps.length === 0 ? <p className="muted small">No labelled entries yet.</p> : (
        <div className="table-scroll"><table className="data-table">
          <thead><tr><th>Entry</th><th>Token</th><th>Launchpad</th><th>Labels</th><th>Entry multiple</th><th>Best within 1 h</th><th>Worst within 1 h</th></tr></thead>
          <tbody>{eps.map((e) => (
            <tr key={`${e.kind}:${e.token}`}>
              <td>{formatDate(e.entry_at)}</td>
              <td className="mono small" title={e.token}>{e.token.slice(0, 6)}…{e.token.slice(-4)}</td>
              <td>{e.launchpad ?? "—"}</td>
              <td className="small">{(e.labels ?? []).map((x: string) => BEHAVIOUR_SHORT[x] ?? x).join(", ")}</td>
              <td>{e.outcome?.entry_multiple === undefined ? "—" : `${num(e.outcome.entry_multiple, 2)}x`}</td>
              <td className="pos">{e.outcome?.max_return_60m_pct === undefined ? "—" : `${num(e.outcome.max_return_60m_pct, 1)}%`}</td>
              <td className="neg">{e.outcome?.min_return_60m_pct === undefined ? "—" : `${num(e.outcome.min_return_60m_pct, 1)}%`}</td>
            </tr>))}
          </tbody></table></div>)}
      <ul className="muted small">{Object.entries((data.definitions ?? {}) as Record<string, string>).map(([k, v]) => <li key={k}>{BEHAVIOUR_SHORT[k] ?? k}: {v}</li>)}</ul>
    </div>
  );
}

function PnlBlock({ st, unit }: { st: J; unit: string }) {
  if (!st || st.closed_trades === null || st.closed_trades === undefined || st.closed_trades === 0) {
    return <p className="small"><span className="pill pill-off">INSUFFICIENT DATA</span> <span className="muted">{(st?.reasons ?? []).join("; ")}</span></p>;
  }
  const e = st.usually_earns, l = st.usually_loses, o = st.outliers ?? {};
  return (
    <div>
      {st.status !== "OK" && <p className="small"><span className="pill pill-warn">INSUFFICIENT DATA</span> <span className="muted">{st.reasons.join("; ")}</span></p>}
      <div className="stat-grid">
        <div className="stat"><div className="stat-label">Usually earns ({unit})</div>
          <div className="stat-value small pos">{e ? `avg +${num(e.avg)} (${num(e.avg_pct, 1)}%) · median +${num(e.median)} (${num(e.median_pct, 1)}%)` : "no winning trades"}</div></div>
        <div className="stat"><div className="stat-label">Usually loses ({unit})</div>
          <div className="stat-value small neg">{l ? `avg ${num(l.avg)} (${num(l.avg_pct, 1)}%) · median ${num(l.median)} (${num(l.median_pct, 1)}%)` : "no losing trades"}</div></div>
        <div className="stat"><div className="stat-label">Closed / wins / losses</div><div className="stat-value small">{st.closed_trades} / {st.winning_trades} / {st.losing_trades} ({pct(st.win_rate)})</div></div>
        <div className="stat"><div className="stat-label">Realized PnL / ROI</div><div className={`stat-value small ${signed(st.realized_pnl)}`}>{num(st.realized_pnl)} {unit} / {st.roi === null ? "—" : `${num(st.roi, 1)}%`}</div></div>
        <div className="stat"><div className="stat-label">Profit factor</div><div className="stat-value small">{st.profit_factor ?? (st.profit_factor_note || "—")}</div></div>
        <div className="stat"><div className="stat-label">Max drawdown ({unit})</div><div className="stat-value small neg">{num(st.max_drawdown)}</div></div>
        <div className="stat"><div className="stat-label">Largest win / loss</div><div className="stat-value small"><span className="pos">{num(e?.largest)}</span> / <span className="neg">{num(l?.largest)}</span></div></div>
        <div className="stat"><div className="stat-label">Hold avg / median</div><div className="stat-value small">{hold(st.avg_hold_s)} / {hold(st.median_hold_s)}</div></div>
        <div className="stat"><div className="stat-label">Outlier dependence</div><div className="stat-value small">{o.dependence ?? "—"}</div>
          <div className="form-hint">without best: {num(o.without_best)} · without top 3: {num(o.without_top3)}</div></div>
      </div>
    </div>
  );
}

function WalletDetail({ p, onRefreshed }: { p: J; onRefreshed?: () => void }) {
  const pnl = p.metrics?.pnl;
  const unit = NATIVE[p.chain] ?? "";
  const external = <>
    {p.chain !== "solana" && <BehaviourDetail chain={p.chain} wallet={p.wallet} />}
    <ExternalDetail chain={p.chain} wallet={p.wallet} ext={p.external} onRefreshed={onRefreshed} />
  </>;
  if (!pnl) return <><p className="muted small">No P/L profile yet (rebuilt every 10 minutes).</p>{external}</>;
  const windows = Object.entries((pnl.windows ?? {}) as Record<string, J>);
  return (
    <div style={{ padding: "8px 0" }}>
      <h4 className="small">All observed history ({pnl.cost_basis ?? "no cost basis"})</h4>
      <PnlBlock st={pnl.all} unit={unit} />
      {windows.length > 0 && (
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Window</th><th>Closed</th><th>Win rate</th><th>Usually earns</th><th>Usually loses</th><th>Profit factor</th><th>Realized</th><th>Status</th></tr></thead>
            <tbody>
              {windows.map(([w, st]) => (
                <tr key={w}>
                  <td>{w}</td><td>{st.closed_trades ?? "—"}</td><td>{pct(st.win_rate)}</td>
                  <td className="pos">{st.usually_earns ? `+${num(st.usually_earns.median)} (${num(st.usually_earns.median_pct, 1)}%)` : "—"}</td>
                  <td className="neg">{st.usually_loses ? `${num(st.usually_loses.median)} (${num(st.usually_loses.median_pct, 1)}%)` : "—"}</td>
                  <td>{st.profit_factor ?? "—"}</td>
                  <td className={signed(st.realized_pnl)}>{num(st.realized_pnl)}</td>
                  <td className="small">{st.status === "OK" ? "OK" : <span title={(st.reasons ?? []).join("; ")}>INSUFFICIENT DATA</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {(pnl.notes ?? []).length > 0 && <ul className="muted small">{pnl.notes.map((n: string) => <li key={n}>{n}</li>)}</ul>}
      <Validation v={p.metrics?.validation} r={p.metrics?.regimes} pf={p.metrics?.paper_follow} />
      {external}
    </div>
  );
}

export default function SmartWalletsPage() {
  const [chain, setChain] = useState("");
  const [sort, setSort] = useState("last_seen");
  const [label, setLabel] = useState("");
  const [stage, setStage] = useState("");
  const [contracts, setContracts] = useState(false);
  const { data, error, loading, reload } = useApi<J>("/api/wallets/profiles",
    { sort, ...(chain ? { chain } : {}), ...(label ? { label } : {}), ...(stage ? { stage } : {}), ...(contracts ? { include_contracts: true } : {}) }, { refreshMs: 60000 });
  const [msg, setMsg] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const watch = async (p: { chain: string; wallet: string }) => {
    try { await apiPost("/api/copy/targets", { chain: p.chain, wallet: p.wallet, mode: "NOTIFY" }); setMsg(`${p.wallet} added as a NOTIFY copy target`); reload(); }
    catch (e) { setMsg(String((e as Error).message)); }
  };
  return (
    <div>
      <PageHeader title="Smart Wallets" icon={<Fingerprint size={20} aria-hidden />}
        subtitle="Observed wallet behaviour on Solana, BSC and Robinhood Chain. Not a ranking: sort by any metric; no wallet is labelled best, and a score needs enough closed trades." />
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", margin: "12px 0" }}>
        <label className="small">Chain <select value={chain} onChange={(e) => setChain(e.target.value)}>{CHAINS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
        <label className="small">Sort by <select value={sort} onChange={(e) => setSort(e.target.value)}>{SORTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
        <label className="small">Behaviour <select value={label} onChange={(e) => setLabel(e.target.value)}>{LABELS.map((l) => <option key={l} value={l}>{l || "Any"}</option>)}</select></label>
        <label className="small">Discovery stage <select value={stage} onChange={(e) => setStage(e.target.value)}>{STAGES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
        <label className="small" title="routers and bots credited with trades: not wallets, never candidates">
          <input type="checkbox" checked={contracts} onChange={(e) => setContracts(e.target.checked)} /> show contract addresses</label>
      </div>
      <ErrorNotice error={error ?? msg} />
      <Section title={`Wallet profiles (sorted by ${SORTS.find((s) => s[0] === sort)?.[1].toLowerCase()})`}>
        <p className="muted small">{data?.note}</p>
        {loading && !data && <Loading />}
        {data && data.profiles.length === 0 && <Empty>No profiles yet. They are rebuilt every 10 minutes by the copy engine from observed trades.</Empty>}
        {data && data.profiles.length > 0 && (
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th /><th>Chain</th><th>Wallet</th><th>Behaviour</th><th>Trades</th><th>Tokens</th><th>Closed</th><th>Win rate</th><th>Realized</th><th>Early entries</th><th>Avg hold</th><th>Score</th><th>Stage</th><th title="labelled entries: successful / failed (EVM)">Entries</th><th>External</th><th>Last seen</th><th /></tr></thead>
              <tbody>
                {data.profiles.map((p: J) => {
                  const k = `${p.chain}:${p.wallet}`;
                  return [
                  <tr key={k}>
                    <td><button className="btn btn-ghost btn-sm" aria-expanded={open === k} aria-label="profit and loss detail"
                      onClick={() => setOpen(open === k ? null : k)}>{open === k ? <ChevronDown size={14} aria-hidden /> : <ChevronRight size={14} aria-hidden />}</button></td>
                    <td>{p.chain}</td>
                    <td className="mono small" title={p.wallet}>{p.wallet.slice(0, 6)}…{p.wallet.slice(-4)}</td>
                    <td className="small">{p.labels.join(", ") || "—"}</td>
                    <td>{p.trades}</td><td>{p.tokens}</td><td>{p.metrics.closed_tokens ?? "—"}</td>
                    <td>{pct(p.metrics.win_rate)}</td>
                    <td className={Number(p.metrics.realized_pnl) > 0 ? "pos" : Number(p.metrics.realized_pnl) < 0 ? "neg" : ""}>
                      {p.source === "evm_trades" ? Number(p.metrics.realized_pnl).toFixed(4) : "—"}</td>
                    <td>{pct(p.metrics.early_entry_share)}</td>
                    <td>{p.metrics.avg_hold_s ? `${Math.round(p.metrics.avg_hold_s)} s` : "—"}</td>
                    <td title={JSON.stringify(p.score_detail?.components ?? p.score_detail)}>{p.score ?? <span className="muted small">insufficient data</span>}</td>
                    <td>{p.metrics.discovery ? <span className={STAGE_CLASS[p.metrics.discovery.stage] ?? "pill pill-off"} title={p.metrics.discovery.reason ?? p.metrics.validation?.reason}>
                      {p.metrics.discovery.stage.replaceAll("_", " ")}</span> : <span className="muted small">—</span>}
                      {p.metrics.stale && <span className="pill pill-warn" title={p.metrics.stale.reason}> STALE</span>}</td>
                    <td><BehaviourSummary b={p.behaviour} /></td>
                    <td><ExternalBadges ext={p.external} /></td>
                    <td>{formatDate(p.last_seen)}</td>
                    <td>{p.is_copy_target ? <span className="muted small">target</span> :
                      <button className="btn btn-ghost btn-sm" onClick={() => watch({ chain: p.chain, wallet: p.wallet })}><Eye size={14} aria-hidden /> Watch</button>}</td>
                  </tr>,
                  open === k && <tr key={`${k}:detail`}><td colSpan={17}><WalletDetail p={p} onRefreshed={reload} /></td></tr>,
                  ];
                })}
              </tbody>
            </table>
          </div>
        )}
      </Section>
      <ExternalPanel onWatch={(chain, wallet) => watch({ chain, wallet })} />
      <ValidationSettings />
    </div>
  );
}
