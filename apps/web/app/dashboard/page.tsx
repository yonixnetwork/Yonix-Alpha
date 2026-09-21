"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiFetch, getAccessToken, logout } from "@/lib/api";

interface SystemStatus {
  app_env: string;
  app_name: string;
  trading_enabled: boolean;
  live_trading_enabled: boolean;
  engines: Record<string, string>;
}

export default function DashboardPage() {
  const router = useRouter();
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    const res = await apiFetch("/api/system/status");
    if (res.status === 401) {
      router.replace("/login");
      return;
    }
    if (!res.ok) {
      setError("Failed to load system status.");
      return;
    }
    setStatus(await res.json());
  }, [router]);

  useEffect(() => {
    if (!getAccessToken()) {
      router.replace("/login");
      return;
    }
    loadStatus();
  }, [loadStatus, router]);

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

      {error && <div className="error" style={{ padding: 24 }}>{error}</div>}

      {status && (
        <div className="status-grid">
          <div className="card">
            <div className="status-label">Environment</div>
            <div className="status-value">{status.app_env}</div>
          </div>
          <div className="card">
            <div className="status-label">Trading Enabled</div>
            <div className="status-value">
              <span className={`pill ${status.trading_enabled ? "pill-ok" : "pill-off"}`}>
                {status.trading_enabled ? "ON" : "OFF"}
              </span>
            </div>
          </div>
          <div className="card">
            <div className="status-label">Live Trading Enabled</div>
            <div className="status-value">
              <span className={`pill ${status.live_trading_enabled ? "pill-ok" : "pill-off"}`}>
                {status.live_trading_enabled ? "ON" : "OFF"}
              </span>
            </div>
          </div>
          {Object.entries(status.engines).map(([name, state]) => (
            <div className="card" key={name}>
              <div className="status-label">{name.replace(/_/g, " ")}</div>
              <div className="status-value">
                <span className="pill pill-off">{state.replace(/_/g, " ")}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
