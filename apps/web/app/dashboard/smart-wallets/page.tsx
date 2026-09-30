"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight, Eye, Fingerprint } from "lucide-react";
import { Empty, ErrorNotice, Loading, PageHeader, Section } from "@/components/ui";
import { apiPost } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const CHAINS: [string, string][] = [["", "All chains"], ["solana", "Solana"], ["bsc", "BSC"], ["robinhood", "Robinhood Chain"]];
const SORTS: [string, string][] = [["last_seen", "Last seen"], ["trades", "Trades"], ["tokens", "Tokens"], ["score", "Score"]];
const LABELS = ["", "SNIPER", "SCALPER", "HOLDER", "HIGH_ACTIVITY", "POSSIBLE_BOT"];
const pct = (v: any) => (v === null || v === undefined ? "—" : `${(Number(v) * 100).toFixed(0)}%`);
const NATIVE: Record<string, string> = { solana: "SOL", bsc: "BNB", robinhood: "ETH" };
const num = (v: any, d = 4) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));
const signed = (v: any) => (v === null || v === undefined ? "" : Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : "");
const hold = (s: any) => (s === null || s === undefined ? "—" : s < 120 ? `${Math.round(s)} s` : s < 7200 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);

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

function WalletDetail({ p }: { p: J }) {
  const pnl = p.metrics?.pnl;
  const unit = NATIVE[p.chain] ?? "";
  if (!pnl) return <p className="muted small">No P/L profile yet (rebuilt every 10 minutes).</p>;
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
    </div>
  );
}

export default function SmartWalletsPage() {
  const [chain, setChain] = useState("");
  const [sort, setSort] = useState("last_seen");
  const [label, setLabel] = useState("");
  const { data, error, loading, reload } = useApi<J>("/api/wallets/profiles",
    { sort, ...(chain ? { chain } : {}), ...(label ? { label } : {}) }, { refreshMs: 60000 });
  const [msg, setMsg] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const watch = async (p: J) => {
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
      </div>
      <ErrorNotice error={error ?? msg} />
      <Section title={`Wallet profiles (sorted by ${SORTS.find((s) => s[0] === sort)?.[1].toLowerCase()})`}>
        <p className="muted small">{data?.note}</p>
        {loading && !data && <Loading />}
        {data && data.profiles.length === 0 && <Empty>No profiles yet. They are rebuilt every 10 minutes by the copy engine from observed trades.</Empty>}
        {data && data.profiles.length > 0 && (
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th /><th>Chain</th><th>Wallet</th><th>Behaviour</th><th>Trades</th><th>Tokens</th><th>Closed</th><th>Win rate</th><th>Realized</th><th>Early entries</th><th>Avg hold</th><th>Score</th><th>Last seen</th><th /></tr></thead>
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
                    <td>{formatDate(p.last_seen)}</td>
                    <td>{p.is_copy_target ? <span className="muted small">target</span> :
                      <button className="btn btn-ghost btn-sm" onClick={() => watch(p)}><Eye size={14} aria-hidden /> Watch</button>}</td>
                  </tr>,
                  open === k && <tr key={`${k}:detail`}><td colSpan={14}><WalletDetail p={p} /></td></tr>,
                  ];
                })}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  );
}
