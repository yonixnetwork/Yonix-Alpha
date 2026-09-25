"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { apiGet, getAccessToken, logout } from "@/lib/api";
import type { KillSwitchStatus, ModesOut } from "@/lib/types";

type NavItem = { href: string; label: string; short: string };

const NAV_GROUPS: { title: string; items: NavItem[] }[] = [
  { title: "", items: [{ href: "/dashboard", label: "Overview", short: "OV" }] },
  {
    title: "Trading",
    items: [
      { href: "/dashboard/decisions", label: "Decisions", short: "DE" },
      { href: "/dashboard/candidates", label: "Candidates", short: "CA" },
      { href: "/dashboard/paper", label: "Paper Trading", short: "PT" },
      { href: "/dashboard/signals", label: "Signals (legacy)", short: "SI" },
    ],
  },
  {
    title: "Risk",
    items: [
      { href: "/dashboard/risk-settings", label: "Risk Settings", short: "RS" },
      { href: "/dashboard/rules", label: "Rules & Blacklist", short: "RB" },
      { href: "/dashboard/risk", label: "Kill Switch & Events", short: "KS" },
    ],
  },
  {
    title: "Operations",
    items: [
      { href: "/dashboard/strategies", label: "Strategy Center", short: "SC" },
      { href: "/dashboard/ml", label: "ML", short: "ML" },
      { href: "/dashboard/events", label: "System Events", short: "EV" },
    ],
  },
];

const COLLAPSE_KEY = "yonixalpha_nav_collapsed";

function isActive(pathname: string, href: string): boolean {
  return href === "/dashboard" ? pathname === href : pathname === href || pathname.startsWith(href + "/");
}

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [modes, setModes] = useState<ModesOut | null>(null);
  const [kill, setKill] = useState<KillSwitchStatus | null>(null);

  useEffect(() => {
    if (!getAccessToken()) router.replace("/login");
    try {
      setCollapsed(localStorage.getItem(COLLAPSE_KEY) === "1");
    } catch {
      /* storage unavailable: default expanded */
    }
  }, [router]);

  useEffect(() => setDrawerOpen(false), [pathname]);

  const refreshStatus = useCallback(async () => {
    try {
      const [m, k] = await Promise.all([apiGet<ModesOut>("/api/control/modes"), apiGet<KillSwitchStatus>("/api/risk/kill-switch")]);
      setModes(m);
      setKill(k);
    } catch {
      /* the badges simply stay unknown; pages report their own errors */
    }
  }, []);

  useEffect(() => {
    refreshStatus();
    const t = setInterval(refreshStatus, 15000);
    return () => clearInterval(t);
  }, [refreshStatus]);

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
      <header className="topbar">
        <div className="topbar-left">
          <button className="icon-btn mobile-only" aria-label="Open navigation" onClick={() => setDrawerOpen(true)}>
            ☰
          </button>
          <button className="icon-btn desktop-only" aria-label="Collapse navigation" onClick={toggleCollapsed}>
            {collapsed ? "»" : "«"}
          </button>
          <div className="brand">YonixAlpha</div>
        </div>
        <div className="topbar-status">
          <span className={modes?.global_mode === "LIVE" ? "pill pill-danger" : "pill pill-ok"} title="Global execution mode">
            {modes ? modes.global_mode : "…"}
          </span>
          <span
            className={modes?.env.live_permitted ? "pill pill-danger" : "pill pill-off"}
            title="Live execution needs TRADING_ENABLED, LIVE_TRADING_ENABLED and PAPER_TRADING=false on the server"
          >
            {modes ? (modes.env.live_permitted ? "LIVE UNLOCKED" : "LIVE LOCKED") : "…"}
          </span>
          <span className={kill?.engaged ? "pill pill-danger" : "pill pill-off"} title={kill?.reason ?? "Kill switch"}>
            {kill ? (kill.engaged ? "KILL SWITCH ON" : "kill switch off") : "…"}
          </span>
          <button className="btn btn-ghost btn-sm" onClick={handleLogout}>
            Sign out
          </button>
        </div>
      </header>
      <div className="dash-body">
        {drawerOpen && <div className="drawer-backdrop" onClick={() => setDrawerOpen(false)} />}
        <nav className={`side-nav ${collapsed ? "collapsed" : ""} ${drawerOpen ? "open" : ""}`} aria-label="Main">
          {NAV_GROUPS.map((group) => (
            <div key={group.title || "root"} className="nav-group">
              {group.title && <div className="nav-group-title">{group.title}</div>}
              {group.items.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  title={item.label}
                  className={`side-nav-link ${isActive(pathname, item.href) ? "active" : ""}`}
                >
                  <span className="nav-short">{item.short}</span>
                  <span className="nav-label">{item.label}</span>
                </Link>
              ))}
            </div>
          ))}
        </nav>
        <main className="dash-main">{children}</main>
      </div>
    </div>
  );
}
