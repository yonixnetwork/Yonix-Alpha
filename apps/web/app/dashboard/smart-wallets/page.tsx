"use client";

import { useState } from "react";
import { Eye, Fingerprint } from "lucide-react";
import { Empty, ErrorNotice, Loading, PageHeader, Section } from "@/components/ui";
import { apiPost } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const CHAINS: [string, string][] = [["", "All chains"], ["solana", "Solana"], ["bsc", "BSC"], ["robinhood", "Robinhood Chain"]];
const SORTS: [string, string][] = [["last_seen", "Last seen"], ["trades", "Trades"], ["tokens", "Tokens"], ["score", "Score"]];
const LABELS = ["", "SNIPER", "SCALPER", "HOLDER", "HIGH_ACTIVITY", "POSSIBLE_BOT"];
const pct = (v: any) => (v === null || v === undefined ? "—" : `${(Number(v) * 100).toFixed(0)}%`);

export default function SmartWalletsPage() {
  const [chain, setChain] = useState("");
  const [sort, setSort] = useState("last_seen");
  const [label, setLabel] = useState("");
  const { data, error, loading, reload } = useApi<J>("/api/wallets/profiles",
    { sort, ...(chain ? { chain } : {}), ...(label ? { label } : {}) }, { refreshMs: 60000 });
  const [msg, setMsg] = useState<string | null>(null);
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
              <thead><tr><th>Chain</th><th>Wallet</th><th>Behaviour</th><th>Trades</th><th>Tokens</th><th>Closed</th><th>Win rate</th><th>Realized</th><th>Early entries</th><th>Avg hold</th><th>Score</th><th>Last seen</th><th /></tr></thead>
              <tbody>
                {data.profiles.map((p: J) => (
                  <tr key={`${p.chain}:${p.wallet}`}>
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
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  );
}
