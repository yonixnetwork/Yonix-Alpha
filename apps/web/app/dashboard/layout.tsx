"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Archive, Bell, BellRing, CandlestickChart, GitCompare, History, KeyRound, LayoutGrid, ListChecks, LogOut, ShieldCheck, Menu, PanelLeftClose, PanelLeftOpen, Server } from "lucide-react";
import { isMemecoinPath, MEMECOIN_ARCHIVED, MEMECOIN_GROUPS, type NavGroup, type NavItem } from "@/components/archivedEngines";
import NotificationsBell from "@/components/NotificationsBell";
import RuntimeApply from "@/components/RuntimeApply";
import { modeClass, stateClass } from "@/components/ui";
import { getAccessToken, logout } from "@/lib/api";
import type { SummaryOut } from "@/lib/cc";
import { EventsProvider, useLiveStatus } from "@/lib/events";
import { accountOn, useProfile } from "@/lib/profile";
import { useApi } from "@/lib/useApi";

// Regular trading is the active navigation. Memecoin Trading is archived:
// its pages stay reachable from Archived Engines and appear in the menu
// while one of them is open (components/archivedEngines).
const ACTIVE_GROUPS: NavGroup[] = [
  {
    // Regular crypto (Bybit / Binance spot, DEMO only): a separate engine
    // from Memecoin Trading (yonixalpha_crypto).
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
  {
    title: "System",
    items: [
      { href: "/dashboard/health", label: "System Health", icon: Server },
      { href: "/dashboard/notifications", label: "Notifications", icon: Bell },
    ],
  },
];

const ARCHIVE_GROUP: NavGroup = {
  title: "Archived Engines",
  items: [{ href: "/dashboard/archived", label: "Memecoin Trading", icon: Archive }],
};

function navGroups(pathname: string): NavGroup[] {
  if (!MEMECOIN_ARCHIVED) return [...MEMECOIN_GROUPS, ...ACTIVE_GROUPS];
  const memecoinOpen = pathname === "/dashboard/archived" || isMemecoinPath(pathname);
  return [...ACTIVE_GROUPS, ARCHIVE_GROUP,
    ...(memecoinOpen ? MEMECOIN_GROUPS.map((g) => ({ ...g, title: `Memecoin${g.title ? ` · ${g.title}` : ""}` })) : [])];
}

const COLLAPSE_KEY = "yonixalpha_nav_collapsed";

function navShown(p: ReturnType<typeof useProfile>, item: NavItem): boolean {
  if (!item.needs) return true;
  if (item.needs === "copy") return p.copyOn;
  if (item.needs === "evm") return p.evmOn;
  return p.chainOn(item.needs);
}

function isActive(pathname: string, href: string): boolean {
  if (href === "/dashboard" || href === "/dashboard/ml" || href === "/dashboard/tokens" || href === "/dashboard/crypto") {
    return pathname === href;
  }
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

function TopbarSummary({ memecoin }: { memecoin: boolean }) {
  const { data } = useApi<SummaryOut>("/api/summary", undefined, {
    refreshMs: 30000,
    reloadOn: ["balance.updated", "trade.created", "trade.closed", "system.health.updated", "notification.created"],
  });
  const profile = useProfile();
  if (!data) return <span className="muted">…</span>;
  return (
    <>
      {memecoin && <>
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
      </>}
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
          <TopbarSummary memecoin={!MEMECOIN_ARCHIVED || isMemecoinPath(pathname)} />
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
          {navGroups(pathname).map((group) => ({ ...group, items: group.items.filter((i) => navShown(profile, i)) }))
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
          {MEMECOIN_ARCHIVED && isMemecoinPath(pathname) && (
            <div className="notice" role="note">
              Archived engine: <strong>Memecoin Trading</strong>. Archiving only hides it from the main menu. Its workers,
              settings and positions are unchanged, and the controls on this page still act on the running Memecoin
              engine. <Link href="/dashboard/archived">About Archived Engines</Link>
            </div>
          )}
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
