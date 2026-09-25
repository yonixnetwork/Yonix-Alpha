"use client";

import { Rocket } from "lucide-react";
import StrategyPage from "@/components/StrategyPage";

export default function MomentumPage() {
  return <StrategyPage name="solana_momentum" icon={<Rocket size={20} aria-hidden />} />;
}
