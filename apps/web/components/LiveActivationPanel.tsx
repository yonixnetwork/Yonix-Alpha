"use client";

import { ShieldAlert, ShieldCheck } from "lucide-react";
import { ErrorNotice } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

export interface LiveBlocker {
  code: string;
  category: string;
  message: string;
  action: string;
  blocks_activation: boolean;
}

export interface LiveActivation {
  state: "LOCKED" | "READY_TO_ACTIVATE" | "ACTIVE_NOT_READY" | "ACTIVE";
  summary: string;
  global_mode: string;
  can_activate: boolean;
  live_orders_possible: boolean;
  locks: Record<string, boolean>;
  locks_open: boolean;
  worker: { status: string; reason: string | null; at: string | null };
  live_strategies: string[];
  eligible_recent: number | null;
  blockers: LiveBlocker[];
  checked_at: string;
}

const CATEGORY: Record<string, string> = {
  LOCKED_BY_SAFETY_POLICY: "Locked by a safety policy",
  WALLET_NOT_CONFIGURED: "Wallet not configured",
  SIGNING_UNAVAILABLE: "Signing unavailable",
  PROVIDER_DISCONNECTED: "Required provider disconnected",
  STRATEGY_DISABLED: "Strategy disabled",
  CONFIGURATION_ERROR: "Configuration error",
  INSUFFICIENT_BALANCE: "Wallet balance",
  STALE_DEPLOYMENT: "Containers out of date",
};

const STATE: Record<LiveActivation["state"], [string, string]> = {
  LOCKED: ["LOCKED", "pill pill-off"],
  READY_TO_ACTIVATE: ["CAN ACTIVATE", "pill pill-warn"],
  ACTIVE_NOT_READY: ["LIVE, NOT READY", "pill pill-warn"],
  ACTIVE: ["LIVE ACTIVE", "pill pill-danger"],
};

/** The server's own answer to "can LIVE be switched on, and would a live
 * order be placed?" (GET /api/control/live-activation). The dashboard
 * never decides this itself. */
export default function LiveActivationPanel({ refreshKey }: { refreshKey?: number }) {
  const a = useApi<LiveActivation>("/api/control/live-activation", { k: refreshKey ?? 0 },
    { refreshMs: 15000, reloadOn: ["strategy.updated"] });
  if (a.error) return <ErrorNotice error={`Live activation status unavailable: ${a.error}`} />;
  const d = a.data;
  if (!d) return null;
  const [label, cls] = STATE[d.state];
  return (
    <div className={d.state === "ACTIVE" ? "card" : "card warn-card"} role="status" aria-live="polite">
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        {d.state === "ACTIVE" ? <ShieldCheck size={16} aria-hidden /> : <ShieldAlert size={16} aria-hidden />}
        <b>Live trading:</b> <span className={cls}>{label}</span>
        <span className="muted small">checked by the server {formatDate(d.checked_at)}</span>
      </div>
      <div style={{ marginTop: 6 }}>{d.summary}</div>
      {d.blockers.length > 0 && (
        <div className="table-wrap" style={{ marginTop: 8 }}>
          <table className="data-table">
            <thead>
              <tr><th>Reason</th><th>What the server found</th><th>What to do</th><th>Stops</th></tr>
            </thead>
            <tbody>
              {d.blockers.map((b) => (
                <tr key={b.code}>
                  <td>{CATEGORY[b.category] ?? b.category}</td>
                  <td>{b.message}</td>
                  <td className="muted small">{b.action}</td>
                  <td>{b.blocks_activation ? <span className="pill pill-danger">the switch</span> : <span className="pill pill-warn">live orders</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="muted small" style={{ marginTop: 6 }}>
        A working switch does not mean a trade happens at once: with LIVE active, only decisions that pass every safety
        check are sent to the wallet. The server&apos;s .env locks, the risk engine and the wallet protections still apply.
      </div>
    </div>
  );
}
