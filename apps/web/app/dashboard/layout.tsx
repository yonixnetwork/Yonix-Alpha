"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ArrowRightLeft, BarChart3, Bell, Boxes, Copy, Fingerprint, FlaskRound, Layers, Link2, Brain, ClipboardCheck, Eye, FlaskConical, Filter, Gauge, LayoutDashboard, ListChecks, LogOut, Menu, PanelLeftClose, PanelLeftOpen, Radio, Rocket, Search, Send, Server, Settings, ShieldAlert, SlidersHorizontal, Network, Sparkles, Wallet, WalletCards, Workflow, type LucideIcon } from "lucide-react";
import NotificationsBell from "@/components/NotificationsBell";
import RuntimeApply from "@/components/RuntimeApply";
import { modeClass, stateClass } from "@/components/ui";
import { getAccessToken, logout } from "@/lib/api";
import type { SummaryOut } from "@/lib/cc";
import { EventsProvider, useLiveStatus } from "@/lib/events";
import { accountOn, useProfile } from "@/lib/profile";
import { useApi } from "@/lib/useApi";

// `needs`: the chain or feature the page belongs to; left out of the menu
// while the system profile switches it off (lib/profile).
type NavItem = { href: string; label: string; icon: LucideIcon; needs?: "bsc" | "robinhood" | "evm" | "copy" };

// YONIXALPHA: Solana, BSC and Robinhood Chain memecoin trading. Legacy
// futures / forex / grid pages are not part of this navigation.
const NAV_GROUPS: { title: string; items: NavItem[] }[] = [
  { title: "", items: [{ href: "/dashboard", label: "Dashboard", icon: LayoutDashboard }] },
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
      { href: "/dashboard/health", label: "System Health", icon: Server },
      { href: "/dashboard/settings", label: "Settings", icon: Settings },
      { href: "/dashboard/rpc", label: "RPC / Data Providers", icon: Network },
      { href: "/dashboard/config", label: "Configuration Health", icon: SlidersHorizontal },
      { href: "/dashboard/notifications", label: "Notifications", icon: Bell },
      { href: "/dashboard/smoke-test", label: "Live Smoke Test", icon: FlaskConical },
    ],
  },
];

const COLLAPSE_KEY = "yonixalpha_nav_collapsed";

function navShown(p: ReturnType<typeof useProfile>, item: NavItem): boolean {
  if (!item.needs) return true;
  if (item.needs === "copy") return p.copyOn;
  if (item.needs === "evm") return p.evmOn;
  return p.chainOn(item.needs);
}

function isActive(pathname: string, href: string): boolean {
  if (href === "/dashboard" || href === "/dashboard/ml" || href === "/dashboard/tokens") return pathname === href;
  return pathname === href || pathname.startsWith(href + "/");
}

function LiveIndicator() {
  const { status } = useLiveStatus();
  const label = status === "live" ? "Live" : status === "connecting" ? "Connecting" : "Offline (polling)";
  return (
    <span className={`live-dot live-${status}`} role="status" aria-live="polite" title={`Realtime updates: ${label}`}>
      <span className="dot" aria-hidden /> {label}
    </span>
  );
}

function TopbarSummary() {
  const { data } = useApi<SummaryOut>("/api/summary", undefined, {
    refreshMs: 30000,
    reloadOn: ["balance.updated", "trade.created", "trade.closed", "system.health.updated", "notification.created"],
  });
  const profile = useProfile();
  if (!data) return <span className="muted">…</span>;
  return (
    <>
      <div className="balances" aria-label="Paper balances">
        {data.accounts
          .filter((a) => ["solana", "live_solana", "evm_bsc", "evm_robinhood"].includes(a.name) && accountOn(profile, a.name))
          .map((a) => {
            const pnl = Number(a.realized_pnl_today);
            return (
              <span key={a.name} className="balance" title={`${a.name}: available ${a.available} ${a.currency}`}>
                <span className="muted">{a.name}</span> {Number(a.balance).toLocaleString(undefined, { maximumFractionDigits: 3 })}{" "}
                {a.currency}
                <span className={pnl > 0 ? "pos" : pnl < 0 ? "neg" : "muted"}>
                  {" "}
                  {pnl >= 0 ? "+" : ""}
                  {pnl.toLocaleString(undefined, { maximumFractionDigits: 4 })} today
                </span>
              </span>
            );
          })}
      </div>
      <Link href="/dashboard/positions" className="pill pill-off" title="Open positions">
        {data.open_positions} open
      </Link>
      <span className={modeClass(data.global_mode)} title="Global execution mode">
        {data.global_mode}
      </span>
      <span className={data.env.live_permitted ? "pill pill-danger" : "pill pill-off"} title="Live execution needs the server's environment locks">
        {data.env.live_permitted ? "LIVE UNLOCKED" : "LIVE LOCKED"}
      </span>
      {data.kill_switch && <span className="pill pill-danger">KILL SWITCH ON</span>}
      <Link href="/dashboard/health" className={stateClass(data.system_status)}
        title={`Worst current connection state. ${notConnected(data.connections) || "Every checked connection is CONNECTED."} Open System Health for details.`}>
        {data.system_status}{data.system_status !== "CONNECTED" && data.connections ? `: ${worstNames(data.connections, data.system_status)}` : ""}
      </Link>
    </>
  );
}

/** "copy-engine STALE, ml UNKNOWN": every connection not CONNECTED (not-configured modules left out). */
function notConnected(conns: Record<string, string> | undefined): string {
  return Object.entries(conns ?? {}).filter(([, st]) => st !== "CONNECTED" && st !== "NOT_CONFIGURED")
    .map(([name, st]) => `${name} ${st}`).join(", ");
}

/** The names in the worst state, at most two, for the badge itself. */
function worstNames(conns: Record<string, string>, worst: string): string {
  const names = Object.entries(conns).filter(([, st]) => st === worst).map(([name]) => name);
  return names.length > 2 ? `${names.slice(0, 2).join(", ")} +${names.length - 2}` : names.join(", ");
}

function Shell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const profile = useProfile();

  useEffect(() => {
    if (!getAccessToken()) router.replace("/login");
    try {
      setCollapsed(localStorage.getItem(COLLAPSE_KEY) === "1");
    } catch {
      /* storage unavailable: default expanded */
    }
  }, [router]);

  useEffect(() => setDrawerOpen(false), [pathname]);
  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setDrawerOpen(false);
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [drawerOpen]);

  function toggleCollapsed() {
    const next = !collapsed;
    setCollapsed(next);
    try {
      localStorage.setItem(COLLAPSE_KEY, next ? "1" : "0");
    } catch {
      /* ignore */
    }
  }

  async function handleLogout() {
    await logout();
    router.replace("/login");
  }

  return (
    <div className="shell">
      <a href="#main" className="skip-link">
        Skip to content
      </a>
      <header className="topbar">
        <div className="topbar-left">
          <button className="icon-btn mobile-only" aria-label="Open navigation" aria-expanded={drawerOpen} onClick={() => setDrawerOpen(true)}>
            <Menu size={16} aria-hidden />
          </button>
          <button
            className="icon-btn desktop-only"
            aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
            aria-pressed={collapsed}
            onClick={toggleCollapsed}
          >
            {collapsed ? <PanelLeftOpen size={16} aria-hidden /> : <PanelLeftClose size={16} aria-hidden />}
          </button>
          <Link href="/dashboard" className="brand">
            YONIXALPHA
          </Link>
          {/* Real global mode is shown by TopbarSummary; this shows whether the
              running services applied the latest dashboard settings. */}
          <RuntimeApply />
        </div>
        <div className="topbar-status">
          <TopbarSummary />
          <LiveIndicator />
          <NotificationsBell />
          <button className="icon-btn" aria-label="Sign out" title="Sign out" onClick={handleLogout}>
            <LogOut size={16} aria-hidden />
          </button>
        </div>
      </header>
      <div className="dash-body">
        {drawerOpen && <div className="drawer-backdrop" onClick={() => setDrawerOpen(false)} aria-hidden />}
        <nav className={`side-nav ${collapsed ? "collapsed" : ""} ${drawerOpen ? "open" : ""}`} aria-label="Main">
          {NAV_GROUPS.map((group) => ({ ...group, items: group.items.filter((i) => navShown(profile, i)) }))
            .filter((group) => group.items.length > 0).map((group) => (
            <div key={group.title || "root"} className="nav-group">
              {group.title && <div className="nav-group-title">{group.title}</div>}
              {group.items.map((item) => {
                const active = isActive(pathname, item.href);
                const Icon = item.icon;
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    title={collapsed ? item.label : undefined}
                    aria-current={active ? "page" : undefined}
                    className={`side-nav-link ${active ? "active" : ""}`}
                  >
                    <Icon size={17} aria-hidden className="nav-icon" />
                    <span className="nav-label">{item.label}</span>
                  </Link>
                );
              })}
            </div>
          ))}
        </nav>
        <main className="dash-main" id="main" tabIndex={-1}>
          {children}
        </main>
      </div>
    </div>
  );
}

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return (
    <EventsProvider>
      <Shell>{children}</Shell>
    </EventsProvider>
  );
}
