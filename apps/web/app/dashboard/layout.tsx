"use client";

import { useEffect } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { getAccessToken, logout } from "@/lib/api";

const NAV_ITEMS = [
  { href: "/dashboard", label: "Overview" },
  { href: "/dashboard/candidates", label: "Candidates" },
  { href: "/dashboard/signals", label: "Signals" },
  { href: "/dashboard/risk", label: "Risk" },
  { href: "/dashboard/ml", label: "ML" },
  { href: "/dashboard/paper", label: "Paper Trading" },
  { href: "/dashboard/events", label: "System Events" },
];

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!getAccessToken()) {
      router.replace("/login");
    }
  }, [router]);

  async function handleLogout() {
    await logout();
    router.replace("/login");
  }

  return (
    <div className="shell">
      <div className="topbar">
        <div className="brand">YonixAlpha</div>
        <button className="btn btn-ghost" onClick={handleLogout}>
          Sign out
        </button>
      </div>
      <div className="dash-body">
        <nav className="side-nav">
          {NAV_ITEMS.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`side-nav-link ${pathname === item.href ? "active" : ""}`}
            >
              {item.label}
            </Link>
          ))}
        </nav>
        <main className="dash-main">{children}</main>
      </div>
    </div>
  );
}
