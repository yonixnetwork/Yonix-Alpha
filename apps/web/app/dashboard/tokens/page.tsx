"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Search } from "lucide-react";
import { ErrorNotice, Loading, PageHeader, Section } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

const MINT = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;

/** Find any token by mint address, or open one of the recently observed. */
export default function TokenExplorerPage() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState("");
  const recent = useApi<{ items: { mint: string; symbol: string | null; name: string | null; outcome: string; decided_at: string }[] }>(
    `/api/observations?limit=50${filter ? `&q=${encodeURIComponent(filter)}` : ""}`, undefined, { refreshMs: 15000 });
  function go() {
    const m = q.trim();
    if (MINT.test(m)) router.push(`/dashboard/tokens/${m}`);
    else setFilter(m.slice(0, 64));
  }
  return (
    <div>
      <PageHeader title="Token Explorer" icon={<Search size={20} aria-hidden />} subtitle="Open any token the platform has seen: live market, activity, decision, links and trading." />
      <div className="search-row">
        <label className="sr-only" htmlFor="mint">Mint address</label>
        <input id="mint" className="mono" placeholder="Mint address, symbol or name" value={q} onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") go(); }} />
        <button className="btn" type="button" onClick={go}>Search</button>
      </div>
      <Section title={filter ? `Observed tokens matching "${filter}"` : "Recently observed"}>
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
