"use client";

import type { ReactNode } from "react";
import DecisionsTable from "@/components/DecisionsTable";
import PerformancePanel from "@/components/PerformancePanel";
import PositionsTable from "@/components/PositionsTable";
import StrategyPanel from "@/components/StrategyPanel";
import { ErrorNotice, Loading, PageHeader, Section } from "@/components/ui";
import type { StrategyOut } from "@/lib/cc";
import { useApi } from "@/lib/useApi";
import { useState } from "react";
import { BuyButton } from "@/components/ManualTrade";

const SOLANA_SOURCE: Record<string, string> = { solana_fresh: "fresh", solana_migration: "migrated", solana_momentum: "momentum" };

/** Manual BUY for any Pump.fun token by mint; the route (bonding curve or
 * PumpSwap) follows the token's real migration state. */
function ManualBuyByMint({ engine, source }: { engine: string; source: string }) {
  const [mint, setMint] = useState("");
  const valid = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(mint.trim());
  return (
    <Section title="Manual BUY">
      <div className="btn-row">
        <label className="sr-only" htmlFor={`buy-${engine}`}>Token mint</label>
        <input id={`buy-${engine}`} placeholder="token mint address" value={mint} onChange={(e) => setMint(e.target.value)} style={{ minWidth: 320 }} />
        {valid && <BuyButton mint={mint.trim()} engine={engine === "solana_migration" ? undefined : engine} source={source} />}
      </div>
      <p className="muted">Or press BUY on a row in the decisions below. Every buy runs the full safety gate; only the strategy
        signal is replaced by your decision.</p>
    </Section>
  );
}

/** The standard page for a strategy or engine: mode + config, live
 * positions with controls, decisions, and paper performance. `children`
 * adds strategy-specific sections above the tables. */
export default function StrategyPage({ name, icon, children, positionsEngine, decisionsEngine }: {
  name: string;
  icon?: ReactNode;
  children?: ReactNode;
  positionsEngine?: string;
  decisionsEngine?: string;
}) {
  const { data: s, error, setData } = useApi<StrategyOut>(`/api/strategies/${name}`, undefined, {
    reloadOn: ["strategy.updated", "trade.closed"],
  });
  if (error) return <ErrorNotice error={error} />;
  if (!s) return <Loading />;
  const futures = s.kind === "futures";
  return (
    <div>
      <PageHeader title={s.label} icon={icon} subtitle={s.account ? `Paper account: ${s.account}` : undefined} />
      <StrategyPanel s={s} onChange={setData} />
      {SOLANA_SOURCE[s.name] && <ManualBuyByMint engine={s.name} source={SOLANA_SOURCE[s.name]} />}
      {children}
      {s.kind !== "analytics" && s.kind !== "grid" && (
        <>
          <Section title="Positions">
            <PositionsTable engine={positionsEngine ?? (futures ? undefined : s.name)} strategy={futures ? s.name : undefined} />
          </Section>
          <Section title="Safety-gate decisions">
            <DecisionsTable engine={decisionsEngine ?? (futures ? undefined : s.name)} strategy={futures ? s.name : undefined} />
          </Section>
          <Section title="Paper performance">
            <PerformancePanel
              account={futures ? undefined : s.account ?? undefined}
              strategy={futures ? s.name : undefined}
              engine={futures ? undefined : s.name}
              hideEmpty={futures}
            />
          </Section>
        </>
      )}
    </div>
  );
}
