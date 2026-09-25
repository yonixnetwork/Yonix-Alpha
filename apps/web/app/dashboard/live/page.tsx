"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { CheckCircle2, Send, XCircle } from "lucide-react";
import FuturesLive from "@/components/FuturesLive";
import { ErrorNotice, Loading, Money, PageHeader, Section, Stat } from "@/components/ui";
import { apiPut, ApiError } from "@/lib/api";
import type { ExecutionOrderRow } from "@/lib/cc";
import { formatDate, formatDecimal } from "@/lib/format";
import { useApi } from "@/lib/useApi";

interface LiveStatus {
  locks: Record<string, boolean>;
  permitted: boolean;
  wallet: { configured: boolean; valid: boolean; pubkey: string | null; error: string | null };
  worker: { status: string; reason: string | null; at: string } | null;
  wallet_sync: { sol: string; at: string; tokens?: number } | null;
  ready: boolean;
  not_ready_reason: string | null;
  global_mode: string;
  checks: { check: string; ok: boolean; detail: string | null }[];
  provider: { name: string; api: string };
  positions: Record<string, number>;
  confirmed_live_orders: number;
  verification: string;
}

interface LiveSettings {
  settings: Record<string, string>;
  limits: Record<string, [string, string]>;
}

interface LivePosition {
  id: string;
  symbol: string;
  mint: string;
  status: string;
  lifecycle: string | null;
  route: string | null;
  remaining: string | null;
  entry_price: string;
  entry_cost_sol: string | null;
  proceeds_sol: string | null;
  last_price: string | null;
  stop_loss: string | null;
  realized_pnl: string | null;
  exit_reason: string | null;
  pending_order_id: string | null;
  exit_failures: number;
  entry_at: string;
}

interface ReconEvent {
  id: string;
  kind: string;
  severity: string;
  mint: string | null;
  position_id: string | null;
  detail: Record<string, unknown> | null;
  created_at: string;
}

const SETTING_LABELS: Record<string, string> = {
  entry_slippage_pct: "Entry slippage %",
  exit_slippage_pct: "Exit slippage %",
  exit_slippage_step_pct: "Extra exit slippage per failed attempt %",
  max_exit_slippage_pct: "Max exit slippage %",
  priority_fee_sol: "Priority fee (SOL)",
  max_priority_fee_sol: "Max priority fee accepted by the guard (SOL)",
  max_platform_fee_bps: "Max provider fee accepted by the guard (bps)",
  min_sol_reserve: "SOL reserve never spent",
  wallet_max_age_seconds: "Max wallet-sync age (s)",
};

function statusPill(status: string): string {
  if (status === "CONFIRMED" || status === "open" || status === "closed") return "pill pill-ok";
  if (["PENDING", "SIGNED", "SUBMITTED", "pending_entry"].includes(status)) return "pill pill-warn";
  if (status === "CANCELLED") return "pill pill-off";
  return "pill pill-danger";
}

function Sig({ sig }: { sig: string | null }) {
  if (!sig) return <span className="muted">—</span>;
  return (
    <a className="mono" href={`https://solscan.io/tx/${sig}`} target="_blank" rel="noreferrer">
      {sig.slice(0, 10)}…
    </a>
  );
}

export default function LiveExecutionPage() {
  const status = useApi<LiveStatus>("/api/live/status", undefined, { refreshMs: 10000 });
  const settings = useApi<LiveSettings>("/api/live/settings");
  const positions = useApi<LivePosition[]>("/api/live/positions", undefined, {
    refreshMs: 15000,
    reloadOn: ["trade.created", "trade.updated", "trade.closed", "position.updated"],
  });
  const orders = useApi<ExecutionOrderRow[]>("/api/live/orders", { limit: 50 }, { refreshMs: 15000 });
  const recon = useApi<ReconEvent[]>("/api/live/reconciliation", { limit: 50 }, { refreshMs: 30000 });
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (settings.data) setDraft(settings.data.settings);
  }, [settings.data]);

  if (status.error) return <ErrorNotice error={status.error} />;
  if (!status.data) return <Loading />;
  const s = status.data;

  async function save() {
    setSaveError(null);
    setSaved(false);
    try {
      const out = await apiPut<LiveSettings>("/api/live/settings", draft);
      settings.setData(out);
      setSaved(true);
    } catch (err) {
      setSaveError(err instanceof ApiError ? err.message : "Save failed.");
    }
  }

  return (
    <div>
      <PageHeader
        title="Live Execution"
        icon={<Send size={20} aria-hidden />}
        subtitle="Pump.fun (PumpPortal local transactions, signed on this server) · Futures & FX (exchange APIs, MT5 bridge)"
      />

      <div className={s.ready ? "card" : "card warn-card"} role="status">
        <b>{s.ready ? "Live execution is READY" : "Live execution is OFF"}</b>
        {!s.ready && s.not_ready_reason && <div className="muted">{s.not_ready_reason}</div>}
        <div style={{ marginTop: 6 }}>
          Verification: <span className="pill pill-warn">{s.verification}</span>
        </div>
        <div className="muted" style={{ marginTop: 6 }}>
          Enabling live execution needs all of: TRADING_ENABLED=true, LIVE_TRADING_ENABLED=true and PAPER_TRADING=false in
          .env; WALLET_PRIVATE_KEY (and optionally WALLET_PUBLIC_KEY) in .env; a Solana RPC; the order worker reporting
          ready; and the global mode set to LIVE. The dashboard cannot change the .env locks.
        </div>
      </div>

      <Section title="Preflight">
        <table className="data-table">
          <thead>
            <tr>
              <th>Check</th>
              <th>State</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {s.checks.map((c) => (
              <tr key={c.check}>
                <td>{c.check}</td>
                <td>
                  {c.ok ? (
                    <span className="pill pill-ok">
                      <CheckCircle2 size={12} aria-hidden /> ok
                    </span>
                  ) : (
                    <span className="pill pill-danger">
                      <XCircle size={12} aria-hidden /> blocking
                    </span>
                  )}
                </td>
                <td className="mono">{c.detail ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="stat-grid" style={{ marginTop: 12 }}>
          {Object.entries(s.locks).map(([k, v]) => (
            <Stat key={k} label={k}>
              <span className={(k === "PAPER_TRADING" ? !v : v) ? "pill pill-danger" : "pill pill-ok"}>{String(v)}</span>
            </Stat>
          ))}
          <Stat label="Wallet">{s.wallet.pubkey ? <span className="mono">{s.wallet.pubkey}</span> : "not configured"}</Stat>
          <Stat label="Wallet SOL (last sync)">
            {s.wallet_sync ? (
              <>
                {formatDecimal(s.wallet_sync.sol, 6)} <span className="muted">{formatDate(s.wallet_sync.at)}</span>
              </>
            ) : (
              "—"
            )}
          </Stat>
          <Stat label="Order worker">{s.worker ? `${s.worker.status}${s.worker.reason ? ` — ${s.worker.reason}` : ""}` : "not running"}</Stat>
          <Stat label="Global mode">{s.global_mode}</Stat>
          <Stat label="Provider">{s.provider.name}</Stat>
        </div>
        <p className="muted">{s.provider.api}.</p>
      </Section>

      <Section title="Execution settings" actions={saved ? <span className="pill pill-ok">saved</span> : undefined}>
        <p className="muted">
          Runtime values, stored in the database and audited. Secrets are never set here. Every transaction is refused
          before signing if it exceeds these bounds or touches anything but a Pump.fun / PumpSwap buy or sell for our wallet.
        </p>
        {saveError && <ErrorNotice error={saveError} />}
        {settings.data ? (
          <div className="form-grid">
            {Object.keys(settings.data.settings).map((k) => (
              <div className="form-row" key={k}>
                <label htmlFor={`live-${k}`}>
                  {SETTING_LABELS[k] ?? k} ({settings.data!.limits[k]?.[0]}–{settings.data!.limits[k]?.[1]})
                </label>
                <input
                  id={`live-${k}`}
                  value={draft[k] ?? ""}
                  onChange={(e) => setDraft({ ...draft, [k]: e.target.value })}
                  inputMode="decimal"
                />
              </div>
            ))}
            <div>
              <button className="btn btn-sm" onClick={save}>
                Save settings
              </button>
            </div>
          </div>
        ) : (
          <Loading />
        )}
      </Section>

      <FuturesLive />

      <Section title="Live positions">
        {!positions.data || positions.data.length === 0 ? (
          <div className="muted">No live positions.</div>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Token</th>
                <th>Status</th>
                <th>Lifecycle / route</th>
                <th>Remaining</th>
                <th>Cost (SOL)</th>
                <th>Proceeds (SOL)</th>
                <th>Realized</th>
                <th>Opened</th>
              </tr>
            </thead>
            <tbody>
              {positions.data.map((p) => (
                <tr key={p.id}>
                  <td>
                    <Link href={`/dashboard/trades/${p.id}`}>{p.symbol}</Link>
                  </td>
                  <td>
                    <span className={statusPill(p.status)}>{p.status}</span>
                    {p.pending_order_id && <div className="muted">order pending</div>}
                    {p.exit_failures > 0 && <div className="neg">{p.exit_failures} failed exit attempt(s)</div>}
                  </td>
                  <td>
                    {p.lifecycle ?? "—"} / {p.route ?? "—"}
                  </td>
                  <td>{formatDecimal(p.remaining, 4)}</td>
                  <td>{formatDecimal(p.entry_cost_sol, 6)}</td>
                  <td>{formatDecimal(p.proceeds_sol, 6)}</td>
                  <td>
                    <Money value={p.realized_pnl} currency="SOL" digits={6} />
                  </td>
                  <td>{formatDate(p.entry_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      <Section title="Orders">
        {orders.error && <ErrorNotice error={orders.error} />}
        {!orders.data || orders.data.length === 0 ? (
          <div className="muted">No orders. An order only exists after the safety gate approved a LIVE trade.</div>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Created</th>
                <th>Side</th>
                <th>Reason</th>
                <th>Status</th>
                <th>Amount</th>
                <th>Route</th>
                <th>Fill (SOL / tokens raw)</th>
                <th>Transaction</th>
              </tr>
            </thead>
            <tbody>
              {orders.data.map((o) => (
                <tr key={o.id}>
                  <td>{formatDate(o.created_at)}</td>
                  <td>{o.side}</td>
                  <td>{o.reason}</td>
                  <td>
                    <span className={statusPill(o.status)}>{o.status}</span>
                    {o.error && <div className="muted">{o.error}</div>}
                  </td>
                  <td>
                    {o.amount} {o.amount_kind}
                  </td>
                  <td>{o.route}</td>
                  <td className="mono">
                    {o.fill ? `${(o.fill.sol_change_lamports / 1e9).toFixed(6)} / ${o.fill.token_change_raw}` : "— (no fill)"}
                  </td>
                  <td>
                    <Sig sig={o.signature} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      <Section title="Reconciliation">
        <p className="muted">
          Wallet vs database, at start-up and every 30 s: SOL balance, orders left signed or submitted, positions whose
          tokens are missing, and holdings no position explains. A mismatch is flagged for review — never resolved by
          inventing an exit.
        </p>
        {!recon.data || recon.data.length === 0 ? (
          <div className="muted">No reconciliation events.</div>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Kind</th>
                <th>Severity</th>
                <th>Token</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {recon.data.map((e) => (
                <tr key={e.id}>
                  <td>{formatDate(e.created_at)}</td>
                  <td>{e.kind.replace(/_/g, " ")}</td>
                  <td>
                    <span className={e.severity === "critical" ? "pill pill-danger" : e.severity === "warning" ? "pill pill-warn" : "pill pill-off"}>
                      {e.severity}
                    </span>
                  </td>
                  <td className="mono">{e.mint ? `${e.mint.slice(0, 8)}…` : "—"}</td>
                  <td className="mono muted" style={{ wordBreak: "break-all" }}>{e.detail ? JSON.stringify(e.detail) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
    </div>
  );
}
