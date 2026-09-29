"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowRightLeft, Eye, Layers, Link2, Rocket, Sparkles } from "lucide-react";
import EvmMarkets from "@/components/EvmMarkets";
import { Empty, ErrorNotice, Loading, PageHeader, Section } from "@/components/ui";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const STATUS_CLASS: Record<string, string> = {
  LIVE: "pill pill-ok", PAPER_ONLY: "pill pill-warn", DEGRADED: "pill pill-danger", UNVERIFIED: "pill pill-off", DISABLED: "pill pill-off",
};

export default function ChainPage() {
  const { chain } = useParams<{ chain: string }>();
  const { data, error, loading } = useApi<J>(`/api/chains/${chain}`, undefined, { refreshMs: 60000, reloadOn: ["controls.updated"] });
  return (
    <div>
      <PageHeader title={data?.name ?? chain} icon={<Link2 size={20} aria-hidden />}
        subtitle={data ? `${data.notes}${data.evm_chain_id ? ` · chain id ${data.evm_chain_id}` : ""}` : undefined} />
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {data && (
        <Section title="Chain and launchpads">
          <p className="small">
            Trading switch: <span className={data.enabled ? "pill pill-ok" : "pill pill-danger"}>{data.enabled ? "ON" : "OFF"}</span>{" "}
            <Link href="/dashboard/launchpads" className="small"><Layers size={14} aria-hidden /> change on Launchpads</Link>
          </p>
          {data.launchpads.length === 0 ? <Empty>No launchpads registered.</Empty> : (
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr><th>Launchpad</th><th>Status</th><th>Why</th><th>Operator mode</th><th>Lifecycle</th></tr></thead>
                <tbody>
                  {data.launchpads.map((lp: J) => (
                    <tr key={lp.key}>
                      <td>{lp.name}</td>
                      <td><span className={STATUS_CLASS[lp.status] ?? "pill pill-off"}>{lp.status === "PAPER_ONLY" ? "PAPER" : lp.status}</span></td>
                      <td className="small">{lp.why}</td>
                      <td>{lp.operator_mode}</td>
                      <td className="small">{lp.lifecycle}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Section>
      )}
      {chain === "solana" ? (
        <Section title="Solana markets">
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Link className="btn btn-ghost btn-sm" href="/dashboard/solana/fresh"><Sparkles size={14} aria-hidden /> Fresh tokens</Link>
            <Link className="btn btn-ghost btn-sm" href="/dashboard/solana/observing"><Eye size={14} aria-hidden /> Observation</Link>
            <Link className="btn btn-ghost btn-sm" href="/dashboard/solana/migrated"><ArrowRightLeft size={14} aria-hidden /> Migrated</Link>
            <Link className="btn btn-ghost btn-sm" href="/dashboard/solana/momentum"><Rocket size={14} aria-hidden /> Momentum</Link>
          </div>
        </Section>
      ) : (chain === "bsc" || chain === "robinhood") && <EvmMarkets fixedChain={chain} header={false} />}
    </div>
  );
}
