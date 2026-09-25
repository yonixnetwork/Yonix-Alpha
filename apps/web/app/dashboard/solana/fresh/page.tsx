"use client";

import { Sparkles } from "lucide-react";
import SolanaFunnel from "@/components/SolanaFunnel";
import StrategyPage from "@/components/StrategyPage";
import { Section } from "@/components/ui";

export default function FreshTokensPage() {
  return (
    <StrategyPage name="solana_fresh" icon={<Sparkles size={20} aria-hidden />}>
      <Section title="Discovery pipeline">
        <SolanaFunnel />
      </Section>
    </StrategyPage>
  );
}
