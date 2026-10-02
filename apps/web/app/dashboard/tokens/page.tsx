"use client";

import Link from "next/link";
import { useState } from "react";
import { Search } from "lucide-react";
import ExplorerActions from "@/components/ExplorerActions";
import { Empty, ErrorNotice, Loading, PageHeader, Section } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const MINT = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const CHAINS: [string, string][] = [["", "All chains"], ["solana", "Solana"], ["bsc", "BSC"], ["robinhood", "Robinhood Chain"]];
const CHAIN_NAME: Record<string, string> = { solana: "Solana", bsc: "BSC", robinhood: "Robinhood Chain" };

/** Search tokens and wallets on Solana, BSC and Robinhood Chain (master
 * §54-55): name / symbol, mint / contract address, creator or wallet. Each
 * result carries the explorer actions of its own chain. */
function SearchResults({ q, chain }: { q: string; chain: string }) {
  const { data, error, loading } = useApi<J>("/api/explorer/search", { q, chain: chain || undefined });
  if (error) return <ErrorNotice error={error} />;
  if (loading && !data) return <Loading />;
  if (!data) return null;
  return (
    <Section title={`Results for "${data.query}" (${data.interpreted_as})`}>
      {data.results.length === 0 && (
        <Empty>
          Nothing found on {data.chains.map((c: string) => CHAIN_NAME[c]).join(", ")}.
          {MINT.test(q) && (!chain || chain === "solana") && <> <Link className="link" href={`/dashboard/tokens/${q}`}>Open the Solana token page anyway</Link> (it reads the mint from the chain).</>}
        </Empty>
      )}
      {data.results.length > 0 && (
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Chain</th><th>Kind</th><th>Token / wallet</th><th>Launchpad</th><th>Match</th><th>Last activity</th><th>Actions</th></tr></thead>
            <tbody>{data.results.map((r: J, i: number) => (
              <tr key={`${r.chain}:${r.kind}:${r.address}:${i}`}>
                <td>{CHAIN_NAME[r.chain] ?? r.chain}</td>
                <td>{r.kind}{r.kind === "wallet" && r.is_copy_target && <span className="pill pill-off"> copy target</span>}</td>
                <td>
                  {r.kind === "token" ? <>{r.symbol ?? "—"} <span className="muted">{r.name ?? ""}</span></> : r.profile ? <>{(r.profile.labels ?? []).join(", ") || "profiled"}{r.profile.stale && <span className="pill pill-warn"> stale</span>}</> : <span className="muted">no profile</span>}
                  <div className="mono small muted">{r.address}</div>
                </td>
                <td>{r.launchpad_name ?? "—"}</td>
                <td className="small">{r.match}{r.kind === "wallet" && r.trades_14d != null ? ` · ${r.trades_14d} trades (14 d)` : ""}</td>
                <td className="muted">{r.last_activity ? formatDate(r.last_activity) : r.profile?.updated_at ? formatDate(r.profile.updated_at) : "—"}</td>
                <td><ExplorerActions links={r.links} unavailable={r.unavailable} explorerName={r.explorer_name} /></td>
              </tr>))}
            </tbody>
          </table>
        </div>
      )}
      <p className="form-hint">{data.note}</p>
    </Section>
  );
}

export default function TokenExplorerPage() {
  const [q, setQ] = useState("");
  const [chain, setChain] = useState("");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("");
  const recent = useApi<{ items: { mint: string; symbol: string | null; name: string | null; outcome: string; decided_at: string }[] }>(
    "/api/observations", { limit: 50, q: filter || undefined }, { refreshMs: 15000 });
  function go() {
    const m = q.trim().slice(0, 64);
    if (m.length < 2) return;
    setQuery(m);
    if (!MINT.test(m) && !m.startsWith("0x")) setFilter(m);
  }
  return (
    <div>
      <PageHeader title="Token Explorer" icon={<Search size={20} aria-hidden />} subtitle="Search tokens and wallets on Solana, BSC and Robinhood Chain: name, symbol, mint / contract address, creator or wallet. Every link is built for the token's own chain." />
      <div className="search-row">
        <label className="sr-only" htmlFor="mint">Token name, symbol, address or wallet</label>
        <input id="mint" className="mono" placeholder="Name, symbol, mint / 0x contract address, creator or wallet" value={q} onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") go(); }} />
        <label className="sr-only" htmlFor="chain">Chain</label>
        <select id="chain" value={chain} onChange={(e) => setChain(e.target.value)}>
          {CHAINS.map(([v, label]) => <option key={v || "all"} value={v}>{label}</option>)}
        </select>
        <button className="btn" type="button" onClick={go}>Search</button>
      </div>
      {query && <SearchResults q={query} chain={chain} />}
      <Section title={filter ? `Observed Solana tokens matching "${filter}"` : "Recently observed (Solana)"}>
        {recent.error ? <ErrorNotice error={recent.error} /> : !recent.data ? <Loading /> : (
          <table className="data-table">
            <thead><tr><th>Token</th><th>Observation outcome</th><th>Decided</th></tr></thead>
            <tbody>{recent.data.items.map((o) => (
              <tr key={o.mint}>
                <td><Link className="link" href={`/dashboard/tokens/${o.mint}`}>{o.symbol ?? o.mint.slice(0, 8)}</Link> <span className="muted">{o.name ?? ""}</span></td>
                <td>{o.outcome}</td><td className="muted">{formatDate(o.decided_at)}</td>
              </tr>))}
            </tbody>
          </table>
        )}
      </Section>
    </div>
  );
}
