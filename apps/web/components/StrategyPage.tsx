"use client";

import type { ReactNode } from "react";
import DecisionsTable from "@/components/DecisionsTable";
import PerformancePanel from "@/components/PerformancePanel";
import PositionsTable from "@/components/PositionsTable";
import StrategyPanel from "@/components/StrategyPanel";
import { ErrorNotice, Loading, PageHeader, Section } from "@/components/ui";
import type { StrategyOut } from "@/lib/cc";
import { useApi } from "@/lib/useApi";

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
