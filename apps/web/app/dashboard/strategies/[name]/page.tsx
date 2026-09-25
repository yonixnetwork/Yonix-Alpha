"use client";

import { useParams } from "next/navigation";
import { Activity, Grid3x3, ListChecks, Scale } from "lucide-react";
import GoldBtcSection from "@/components/GoldBtcSection";
import GridSection from "@/components/GridSection";
import StrategyPage from "@/components/StrategyPage";
import type { StrategyOut } from "@/lib/cc";
import { useApi } from "@/lib/useApi";

const ICONS: Record<string, React.ReactNode> = {
  meta_muse: <Activity size={20} aria-hidden />,
  confluence_matrix: <ListChecks size={20} aria-hidden />,
  hyperliquid_grid: <Grid3x3 size={20} aria-hidden />,
  gold_vs_btc: <Scale size={20} aria-hidden />,
};

function GridExtras() {
  const { data, reload } = useApi<StrategyOut>("/api/strategies/hyperliquid_grid", undefined, {
    reloadOn: ["strategy.updated", "trade.updated"],
    refreshMs: 20000,
  });
  return data ? <GridSection s={data} reload={reload} /> : null;
}

export default function StrategyDetailPage() {
  const { name } = useParams<{ name: string }>();
  return (
    <StrategyPage name={name} icon={ICONS[name]}>
      {name === "hyperliquid_grid" && <GridExtras />}
      {name === "gold_vs_btc" && <GoldBtcSection />}
    </StrategyPage>
  );
}
