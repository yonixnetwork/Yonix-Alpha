"use client";

import { ListChecks } from "lucide-react";
import PositionsTable from "@/components/PositionsTable";
import { PageHeader, Section } from "@/components/ui";

/** Closed and open trades; each row opens its Trade Details (execution
 * latency, price analysis, timeline). */
export default function TradesPage() {
  return (
    <div>
      <PageHeader title="Trades" icon={<ListChecks size={20} aria-hidden />} subtitle="Open a trade for its execution latency, price analysis and timeline." />
      <Section title="Live trades">
        <PositionsTable account="live_solana" status="" title="Live trades" />
      </Section>
      <Section title="Paper trades">
        <PositionsTable account="solana" status="" title="Paper trades" />
      </Section>
    </div>
  );
}
