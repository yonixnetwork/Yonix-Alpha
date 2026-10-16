// Archived Engines: engines kept out of the active navigation (2026-10-16).
//
// Archiving is a presentation change only. The Memecoin Trading workers,
// settings, risk limits, positions and history are untouched and keep doing
// whatever they did before; its pages and controls stay reachable from
// Archived Engines. Hiding an engine from the menu is NOT disabling its new
// entries (Memecoin: strategy / global mode OFF) and NOT stopping its workers
// (docker compose stop): those are separate, deliberate operator actions.
//
// Restore: set MEMECOIN_ARCHIVED to false. Its groups then return to the
// main navigation exactly as they were.

import { ArrowRightLeft, BarChart3, Boxes, Brain, ClipboardCheck, Copy, Eye, Filter, Fingerprint, FlaskConical, FlaskRound,
  Gauge, LayoutDashboard, Layers, Link2, ListChecks, Network, Radio, Rocket, Search, Send, Settings, ShieldAlert,
  SlidersHorizontal, Sparkles, Timer, Wallet, WalletCards, Workflow, type LucideIcon } from "lucide-react";

// `needs`: the chain or feature the page belongs to; left out of the menu
// while the system profile switches it off (lib/profile).
export type NavItem = { href: string; label: string; icon: LucideIcon; needs?: "bsc" | "robinhood" | "evm" | "copy" };
export type NavGroup = { title: string; items: NavItem[] };

export const MEMECOIN_ARCHIVED = true;

// The Memecoin Trading pages (Solana, BSC and Robinhood Chain), grouped as
// they were in the main navigation.
export const MEMECOIN_GROUPS: NavGroup[] = [
  { title: "", items: [{ href: "/dashboard/memecoin", label: "Memecoin Overview", icon: LayoutDashboard }] },
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
      { href: "/dashboard/solana/fresh", label: "Fresh Tokens", icon: Sparkles },
      { href: "/dashboard/solana/observing", label: "Observation", icon: Eye },
      { href: "/dashboard/solana/migrated", label: "Migrated", icon: ArrowRightLeft },
      { href: "/dashboard/solana/momentum", label: "Momentum", icon: Rocket },
      { href: "/dashboard/entry-intel", label: "Entry Intelligence", icon: Timer },
      { href: "/dashboard/evm", label: "EVM Markets", icon: Boxes, needs: "evm" },
      { href: "/dashboard/tokens", label: "Token Explorer", icon: Search },
      { href: "/dashboard/launchpads", label: "Launchpads", icon: Layers },
    ],
  },
  {
    title: "Wallet intelligence",
    items: [
      { href: "/dashboard/copy", label: "Copy Trading", icon: Copy, needs: "copy" },
      { href: "/dashboard/smart-wallets", label: "Smart Wallets", icon: Fingerprint, needs: "copy" },
    ],
  },
  {
    title: "Trading",
    items: [
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
      { href: "/dashboard/ml", label: "ML Engine", icon: Brain },
      { href: "/dashboard/ml/review", label: "ML Review", icon: Gauge },
    ],
  },
  {
    title: "Risk",
    items: [
      { href: "/dashboard/risk-settings", label: "Risk", icon: ShieldAlert },
      { href: "/dashboard/rules", label: "Filters", icon: Filter },
      { href: "/dashboard/risk", label: "Kill Switch", icon: Radio },
    ],
  },
  {
    title: "System",
    items: [
      { href: "/dashboard/settings", label: "Settings", icon: Settings },
      { href: "/dashboard/rpc", label: "RPC / Data Providers", icon: Network },
      { href: "/dashboard/config", label: "Configuration Health", icon: SlidersHorizontal },
      { href: "/dashboard/smoke-test", label: "Live Smoke Test", icon: FlaskConical },
    ],
  },
];

// Pages that belong to the active (regular trading) navigation; every other
// dashboard page belongs to the archived Memecoin engine.
const ACTIVE_PATHS = ["/dashboard/crypto", "/dashboard/health", "/dashboard/notifications", "/dashboard/archived"];

export function isMemecoinPath(pathname: string): boolean {
  if (!pathname.startsWith("/dashboard") || pathname === "/dashboard") return false;
  return !ACTIVE_PATHS.some((p) => pathname === p || pathname.startsWith(p + "/"));
}
