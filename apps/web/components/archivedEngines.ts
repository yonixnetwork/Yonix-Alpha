// Active and archived engines in the navigation.
//
// 2026-10-20 (operator request): the product is Solana memecoin trading
// again. Memecoin Trading is the active navigation; regular crypto /
// exchange trading (yonixalpha_crypto: Strategy Center, exchange
// connections, exchange trades, backtests) is archived.
//
// Archiving is a presentation change only. No code, table, setting or
// history is deleted, and no worker is started or stopped by it. The
// archived pages stay reachable from Archived Engines, and their controls
// still act on the Crypto Trading worker (turn its engine off there).
// A snapshot of the exchange work is kept on the branch
// archive/crypto-exchange-trading-2026-10-20.
//
// Restore: set CRYPTO_ARCHIVED to false; its groups then return to the main
// navigation after the Memecoin groups. The 2026-10-16 layout (Crypto
// active, Memecoin archived) is on the archive branch above.

import { ArrowRightLeft, BarChart3, Bell, BellRing, Boxes, Brain, CandlestickChart, ClipboardCheck, Copy, Eye, Filter,
  Fingerprint, FlaskConical, FlaskRound, Gauge, GitCompare, History, KeyRound, LayoutDashboard, LayoutGrid, Layers, Link2,
  ListChecks, Network, Radio, Rocket, Search, Send, Server, Settings, ShieldAlert, ShieldCheck, SlidersHorizontal, Sparkles,
  Timer, Wallet, WalletCards, Workflow, type LucideIcon } from "lucide-react";

// `needs`: the chain or feature the page belongs to; left out of the menu
// while the system profile switches it off (lib/profile).
export type NavItem = { href: string; label: string; icon: LucideIcon; needs?: "bsc" | "robinhood" | "evm" | "copy" };
export type NavGroup = { title: string; items: NavItem[] };

export const CRYPTO_ARCHIVED = true;

// The Memecoin Trading pages (Solana; BSC and Robinhood Chain only while the
// system profile turns them on): the main navigation.
export const MEMECOIN_GROUPS: NavGroup[] = [
  { title: "", items: [{ href: "/dashboard/memecoin", label: "Dashboard", icon: LayoutDashboard }] },
  {
    title: "Chains",
    items: [
      { href: "/dashboard/chains/solana", label: "Solana", icon: Link2 },
      { href: "/dashboard/chains/bsc", label: "BSC", icon: Link2, needs: "bsc" },
      { href: "/dashboard/chains/robinhood", label: "Robinhood Chain", icon: Link2, needs: "robinhood" },
    ],
  },
  {
    title: "Market",
    items: [
      { href: "/dashboard/solana/fresh", label: "Fresh / Early Tokens", icon: Sparkles },
      { href: "/dashboard/solana/observing", label: "Observation", icon: Eye },
      { href: "/dashboard/solana/migrated", label: "Migrated Tokens", icon: ArrowRightLeft },
      { href: "/dashboard/solana/momentum", label: "Momentum", icon: Rocket },
      { href: "/dashboard/entry-intel", label: "Entry Intelligence", icon: Timer },
      { href: "/dashboard/evm", label: "EVM Markets", icon: Boxes, needs: "evm" },
      { href: "/dashboard/tokens", label: "Token Scanner", icon: Search },
      { href: "/dashboard/launchpads", label: "Launchpads", icon: Layers },
    ],
  },
  {
    title: "Wallet intelligence",
    items: [
      { href: "/dashboard/copy", label: "Copy Trading", icon: Copy, needs: "copy" },
      { href: "/dashboard/smart-wallets", label: "Smart Wallets", icon: Fingerprint },
    ],
  },
  {
    title: "Trading",
    items: [
      { href: "/dashboard/strategies", label: "Strategies", icon: Layers },
      { href: "/dashboard/strategy-comparison", label: "Strategy Comparison", icon: GitCompare },
      { href: "/dashboard/paper", label: "Paper Trading", icon: FlaskRound },
      { href: "/dashboard/performance", label: "Solana Performance", icon: BarChart3 },
      { href: "/dashboard/positions", label: "Positions", icon: Wallet },
      { href: "/dashboard/trades", label: "Trade History", icon: ListChecks },
      { href: "/dashboard/decisions", label: "Decisions", icon: ClipboardCheck },
      { href: "/dashboard/funnel", label: "Execution Funnel", icon: Workflow },
      { href: "/dashboard/live", label: "Live Execution", icon: Send },
      { href: "/dashboard/wallets", label: "Wallets", icon: WalletCards },
    ],
  },
  {
    title: "Machine learning",
    items: [
      { href: "/dashboard/ml", label: "ML Intelligence", icon: Brain },
      { href: "/dashboard/ml/review", label: "ML Review", icon: Gauge },
    ],
  },
  {
    title: "Risk",
    items: [
      { href: "/dashboard/risk-settings", label: "Risk Management", icon: ShieldAlert },
      { href: "/dashboard/rules", label: "Filters", icon: Filter },
      { href: "/dashboard/risk", label: "Kill Switch", icon: Radio },
    ],
  },
  {
    title: "System",
    items: [
      { href: "/dashboard/health", label: "System Health", icon: Server },
      { href: "/dashboard/notifications", label: "Notifications", icon: Bell },
      { href: "/dashboard/settings", label: "Settings", icon: Settings },
      { href: "/dashboard/rpc", label: "RPC / Data Providers", icon: Network },
      { href: "/dashboard/config", label: "Configuration Health", icon: SlidersHorizontal },
      { href: "/dashboard/smoke-test", label: "Live Smoke Test", icon: FlaskConical },
    ],
  },
];

// The archived regular crypto / exchange trading pages (yonixalpha_crypto).
export const CRYPTO_GROUPS: NavGroup[] = [
  {
    title: "Crypto Trading",
    items: [
      { href: "/dashboard/crypto/center", label: "Strategy Center", icon: LayoutGrid },
      { href: "/dashboard/crypto", label: "Engine (Strategy Lab)", icon: CandlestickChart },
      { href: "/dashboard/crypto/backtests", label: "Backtests", icon: History },
      { href: "/dashboard/crypto/compare", label: "Compare", icon: GitCompare },
      { href: "/dashboard/crypto/portfolio", label: "Portfolio Risk", icon: ShieldCheck },
      { href: "/dashboard/crypto/trades", label: "Exchange Trades", icon: ListChecks },
      { href: "/dashboard/crypto/connections", label: "Exchange Connections", icon: KeyRound },
      { href: "/dashboard/crypto/alerts", label: "Alerts and Audit", icon: BellRing },
    ],
  },
];

export function isCryptoPath(pathname: string): boolean {
  return pathname === "/dashboard/crypto" || pathname.startsWith("/dashboard/crypto/");
}

// Every dashboard page that is not a Crypto Trading page (or Archived
// Engines itself) belongs to Memecoin Trading.
export function isMemecoinPath(pathname: string): boolean {
  if (!pathname.startsWith("/dashboard") || isCryptoPath(pathname) || pathname === "/dashboard/archived") return false;
  return true;
}
