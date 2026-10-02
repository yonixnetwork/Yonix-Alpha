"use client";

import { WalletCards } from "lucide-react";
import EvmWalletPanel from "@/components/EvmWalletPanel";
import LiveWalletsPanel from "@/components/LiveWalletsPanel";
import TradingWallet from "@/components/TradingWallet";
import { PageHeader } from "@/components/ui";

export default function WalletsPage() {
  return (
    <div>
      <PageHeader title="Wallets" icon={<WalletCards size={20} aria-hidden />}
        subtitle="The real wallet (read from the chain, public address only) and the paper book, side by side and never mixed." />
      <TradingWallet />
      <LiveWalletsPanel />
      <EvmWalletPanel />
    </div>
  );
}
