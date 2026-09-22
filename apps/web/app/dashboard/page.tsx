"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiGet, apiPost, ApiError } from "@/lib/api";
import type { KillSwitchStatus, SystemStatusOut } from "@/lib/types";

const SERVICE_LABELS: Record<string, string> = {
  "data-solana": "Data: Solana",
  "data-binance": "Data: Binance",
  "engine-solana-discovery": "Engine A: Discovery",
  "engine-solana-migration": "Engine B: Migration",
  "engine-solana-momentum": "Engine C: Momentum",
  "engine-binance-futures": "Engine: Binance Futures",
  "decision-engine": "Decision Engine",
  ml: "ML Training",
  "paper-trading": "Paper Trading",
};

function statusPillClass(status: string): string {
  if (status === "running") return "pill pill-ok";
  if (status === "stopped") return "pill pill-danger";
  return "pill pill-off";
}

export default function OverviewPage() {
  const router = useRouter();
  const [status, setStatus] = useState<SystemStatusOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await apiGet<SystemStatusOut>("/api/system/status");
      setStatus(data);
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.replace("/login");
        return;
      }
      setError("Failed to load system status.");
    }
  }, [router]);

  useEffect(() => {
    load();
  }, [load]);

  async function handleEngage() {
    if (!reason.trim()) return;
    setBusy(true);
    try {
      const result = await apiPost<KillSwitchStatus>("/api/risk/kill-switch/engage", { reason });
      setStatus((prev) => (prev ? { ...prev, kill_switch: result } : prev));
      setReason("");
    } catch {
      setError("Failed to engage kill switch.");
    } finally {
      setBusy(false);
    }
  }

  async function handleDisengage() {
    setBusy(true);
    try {
      const result = await apiPost<KillSwitchStatus>("/api/risk/kill-switch/disengage");
      setStatus((prev) => (prev ? { ...prev, kill_switch: result } : prev));
    } catch {
      setError("Failed to disengage kill switch.");
    } finally {
      setBusy(false);
    }
  }

  if (error) return <div className="error">{error}</div>;
  if (!status) return <div className="empty-state">Loading...</div>;

  return (
    <div>
      <div className="page-header">
        <div className="page-title">Overview</div>
      </div>

      <div className="detail-grid">
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
        <div className="card kill-switch-panel">
          <div className="status-label">Kill Switch</div>
          <div className="status-value">
            <span className={`pill ${status.kill_switch.engaged ? "pill-danger" : "pill-ok"}`}>
              {status.kill_switch.engaged ? "ENGAGED" : "CLEAR"}
            </span>
          </div>
          {status.kill_switch.reason && (
            <div style={{ fontSize: 12, color: "var(--text-dim)" }}>{status.kill_switch.reason}</div>
          )}
          {status.kill_switch.engaged ? (
            <button className="btn" onClick={handleDisengage} disabled={busy}>
              Disengage
            </button>
          ) : (
            <>
              <textarea
                placeholder="Reason for engaging the kill switch (required)"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
              <button className="btn btn-danger" onClick={handleEngage} disabled={busy || !reason.trim()}>
                Engage kill switch
              </button>
            </>
          )}
        </div>
      </div>

      <div className="page-title" style={{ fontSize: 16, marginBottom: 12 }}>
        Services
      </div>
      <div className="status-grid" style={{ padding: 0 }}>
        {Object.entries(status.services).map(([name, s]) => (
          <div className="card" key={name}>
            <div className="status-label">{SERVICE_LABELS[name] ?? name}</div>
            <div className="status-value">
              <span className={statusPillClass(s.status)}>{s.status}</span>
            </div>
            {s.last_event_at && (
              <div style={{ fontSize: 11, color: "var(--text-dim)", marginTop: 6 }}>
                {new Date(s.last_event_at).toLocaleString()}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
