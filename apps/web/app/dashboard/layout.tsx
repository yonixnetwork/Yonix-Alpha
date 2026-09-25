"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  Activity,
  ArrowRightLeft,
  Bell,
  Brain,
  CandlestickChart,
  ClipboardCheck,
  Coins,
  Filter,
  Gauge,
  Grid3x3,
  LayoutDashboard,
  Layers,
  ListChecks,
  LogOut,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  Radio,
  Rocket,
  Scale,
  Server,
  Settings,
  ShieldAlert,
  Sparkles,
  TrendingUp,
  Wallet,
  Waves,
  type LucideIcon,
} from "lucide-react";
import NotificationsBell from "@/components/NotificationsBell";
import { modeClass, stateClass } from "@/components/ui";
import { getAccessToken, logout } from "@/lib/api";
import type { SummaryOut } from "@/lib/cc";
import { EventsProvider, useLiveStatus } from "@/lib/events";
import { useApi } from "@/lib/useApi";

type NavItem = { href: string; label: string; icon: LucideIcon };

const NAV_GROUPS: { title: string; items: NavItem[] }[] = [
  { title: "", items: [{ href: "/dashboard", label: "Dashboard", icon: LayoutDashboard }] },
  {
    title: "Solana",
    items: [
      { href: "/dashboard/solana/fresh", label: "Fresh Tokens", icon: Sparkles },
      { href: "/dashboard/solana/migrated", label: "Migrated Tokens", icon: ArrowRightLeft },
      { href: "/dashboard/solana/momentum", label: "Momentum", icon: Rocket },
    ],
  },
  {
    title: "Futures",
    items: [
      { href: "/dashboard/venues/binance", label: "Binance Futures", icon: CandlestickChart },
      { href: "/dashboard/venues/bybit", label: "Bybit", icon: TrendingUp },
      { href: "/dashboard/venues/hyperliquid", label: "Hyperliquid", icon: Waves },
    ],
  },
  {
    title: "Strategies",
    items: [
      { href: "/dashboard/strategies", label: "All Strategies", icon: Layers },
      { href: "/dashboard/strategies/meta_muse", label: "Meta Muse", icon: Activity },
      { href: "/dashboard/strategies/confluence_matrix", label: "Confluence Matrix", icon: ListChecks },
      { href: "/dashboard/strategies/hyperliquid_grid", label: "Hyperliquid Grid", icon: Grid3x3 },
      { href: "/dashboard/strategies/gold_vs_btc", label: "Gold vs BTC", icon: Scale },
    ],
  },
  {
    title: "Trading",
    items: [
      { href: "/dashboard/decisions", label: "Decisions", icon: ClipboardCheck },
      { href: "/dashboard/paper", label: "Paper Trading", icon: Wallet },
      { href: "/dashboard/candidates", label: "Candidates", icon: Coins },
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
      { href: "/dashboard/risk-settings", label: "Risk Settings", icon: ShieldAlert },
      { href: "/dashboard/rules", label: "Blacklist & Filters", icon: Filter },
      { href: "/dashboard/risk", label: "Kill Switch", icon: Radio },
    ],
  },
  {
    title: "System",
    items: [
      { href: "/dashboard/health", label: "System Health", icon: Server },
      { href: "/dashboard/notifications", label: "Notifications", icon: Bell },
      { href: "/dashboard/settings", label: "Settings", icon: Settings },
    ],
  },
];

const COLLAPSE_KEY = "yonixalpha_nav_collapsed";

function isActive(pathname: string, href: string): boolean {
  if (href === "/dashboard" || href === "/dashboard/strategies" || href === "/dashboard/ml") return pathname === href;
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
  if (!data) return <span className="muted">…</span>;
  return (
    <>
      <div className="balances" aria-label="Paper balances">
        {data.accounts
          .filter((a) => a.open_positions > 0 || a.name === "solana" || a.name === "binance_futures")
          .map((a) => {
            const pnl = Number(a.realized_pnl_today);
            return (
              <span key={a.name} className="balance" title={`${a.name}: available ${a.available} ${a.currency}`}>
                <span className="muted">{a.name.replace("_futures", "")}</span> {Number(a.balance).toLocaleString(undefined, { maximumFractionDigits: 3 })}{" "}
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
      <Link href="/dashboard/paper" className="pill pill-off" title="Open paper positions">
        {data.open_positions} open
      </Link>
      <span className={modeClass(data.global_mode)} title="Global execution mode">
        {data.global_mode}
      </span>
      <span className={data.env.live_permitted ? "pill pill-danger" : "pill pill-off"} title="Live execution needs the server's environment locks">
        {data.env.live_permitted ? "LIVE UNLOCKED" : "LIVE LOCKED"}
      </span>
      {data.kill_switch && <span className="pill pill-danger">KILL SWITCH ON</span>}
      <Link href="/dashboard/health" className={stateClass(data.system_status)} title="Worst current connection state">
        {data.system_status}
      </Link>
    </>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);

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
            YonixAlpha
          </Link>
          <span className="pill pill-warn" title="All execution is simulated">
            PAPER
          </span>
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
          {NAV_GROUPS.map((group) => (
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
