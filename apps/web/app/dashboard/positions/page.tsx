"use client";

import { Wallet } from "lucide-react";
import PositionsTable from "@/components/PositionsTable";
import { PageHeader, Section } from "@/components/ui";

/** Every Solana position (live and paper), open by default. */
export default function PositionsPage() {
  return (
    <div>
      <PageHeader title="Open Positions" icon={<Wallet size={20} aria-hidden />} subtitle="Live and paper Solana positions; the stop loss keeps working while management is paused." />
      <Section title="Positions">
        <PositionsTable account="solana" title="Paper positions" />
      </Section>
      <Section title="Live positions">
        <PositionsTable account="live_solana" title="Live positions" />
      </Section>
    </div>
  );
}
