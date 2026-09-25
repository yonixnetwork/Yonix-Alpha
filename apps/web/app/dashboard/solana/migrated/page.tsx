"use client";

import { ArrowRightLeft } from "lucide-react";
import StrategyPage from "@/components/StrategyPage";

export default function MigratedTokensPage() {
  return <StrategyPage name="solana_migration" icon={<ArrowRightLeft size={20} aria-hidden />} />;
}
