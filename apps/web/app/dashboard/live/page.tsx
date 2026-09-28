"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { CheckCircle2, Send, XCircle } from "lucide-react";
import FuturesLive from "@/components/FuturesLive";
import LiveWalletsPanel from "@/components/LiveWalletsPanel";
import SmokeTestPanel from "@/components/SmokeTestPanel";
import { ErrorNotice, fmtDuration, Loading, Money, PageHeader, Section, Stat } from "@/components/ui";
import { apiPut, ApiError } from "@/lib/api";
import type { ExecutionOrderRow } from "@/lib/cc";
import { formatDate, formatDecimal } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import RuntimeApply from "@/components/RuntimeApply";
import { SellButton } from "@/components/ManualTrade";

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
  exit_requested: boolean;
  entry_at: string;
  current_price: string | null;
  price_status: string;
  price_at: string | null;
  price_age_seconds: number | null;
  current_value: string | null;
  unrealized_pnl: string | null;
  unrealized_pnl_pct: string | null;
  take_profits: string[];
  tp_hits: number[];
  trailing_stop: string | null;
  execution_provider: string | null;
  entry_signature: string | null;
  smoke_test_run: string | null;
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
  tx_builder: "Pump transaction builder",
  compute_unit_limit_curve: "Compute-unit limit, bonding curve (measured use ~96k)",
  compute_unit_limit_amm: "Compute-unit limit, PumpSwap (measured use ~142k)",
  auto_reclaim_rent: "Close the token account after a full exit (returns its rent deposit)",
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
    refreshMs: 5000,
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

      <LiveWalletsPanel />
      <SmokeTestPanel />

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

      <Section title="Execution settings" actions={saved ? <RuntimeApply inline /> : undefined}>
        <p className="muted">
          Runtime values, stored in the database and audited. Secrets are never set here. Every transaction is refused
          before signing if it exceeds these bounds or does anything but the requested buy or sell for our wallet (Pump.fun curve, PumpSwap or a Jupiter route, each checked against its own layout).
        </p>
        {saveError && <ErrorNotice error={saveError} />}
        {settings.data ? (
          <div className="form-grid">
            {Object.keys(settings.data.settings).map((k) => k === "tx_builder" ? (
              <div className="form-row" key={k}>
                <label htmlFor="live-tx_builder">{SETTING_LABELS[k]}</label>
                <select id="live-tx_builder" value={draft[k] ?? "native"} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })}>
                  <option value="native">native — built here to the official Pump layouts, no platform fee (recommended)</option>
                  <option value="pumpportal">PumpPortal — third-party builder, 0.5% fee; always checked by the guard</option>
                </select>
                <span className="form-hint">Both are checked by the transaction guard before signing. Non-Pump tokens always use
                  Jupiter; the venue (bonding curve / PumpSwap / Jupiter) is read from chain state for every order.</span>
              </div>
            ) : k === "auto_reclaim_rent" ? (
              <div className="form-row" key={k}>
                <label htmlFor="live-auto_reclaim_rent">{SETTING_LABELS[k]}</label>
                <select id="live-auto_reclaim_rent" value={draft[k] ?? "true"} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })}>
                  <option value="true">on: close the empty account after every full exit</option>
                  <option value="false">off: leave token accounts open</option>
                </select>
                <span className="form-hint">Each buy deposits about 0.0015 SOL of rent into the token account. Closing the empty
                  account returns it; only accounts holding zero tokens are closed, always back to this wallet.</span>
              </div>
            ) : (
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
                <th>Entry → current</th>
                <th>Remaining</th>
                <th>Cost → value (SOL)</th>
                <th>Unrealized</th>
                <th>Realized</th>
                <th>Stop / TPs / trailing</th>
                <th>Opened</th>
                <th>Action</th>
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
                    <div className="muted">{p.execution_provider}{p.smoke_test_run ? " · smoke test" : ""}</div>
                  </td>
                  <td>
                    {p.entry_price} → {p.current_price ?? "—"}
                    <div>
                      <span className={p.price_status === "LIVE" ? "pill pill-ok" : "pill pill-warn"}>{p.price_status}</span>{" "}
                      <span className="muted">{p.price_age_seconds !== null ? `${p.price_age_seconds}s old` : ""}</span>
                    </div>
                  </td>
                  <td>{formatDecimal(p.remaining, 4)}</td>
                  <td>
                    {formatDecimal(p.entry_cost_sol, 6)} → {formatDecimal(p.current_value, 6)}
                    {p.proceeds_sol && p.proceeds_sol !== "0" && <div className="muted">proceeds {formatDecimal(p.proceeds_sol, 6)}</div>}
                  </td>
                  <td>
                    {p.status === "open" ? (
                      <>
                        <Money value={p.unrealized_pnl} currency="SOL" digits={6} />
                        <div className={Number(p.unrealized_pnl_pct) >= 0 ? "pos" : "neg"}>
                          {p.unrealized_pnl_pct !== null ? `${Number(p.unrealized_pnl_pct) >= 0 ? "+" : ""}${p.unrealized_pnl_pct}%` : "—"}
                          {p.price_status !== "LIVE" && " (STALE)"}
                        </div>
                      </>
                    ) : "—"}
                  </td>
                  <td>
                    <Money value={p.realized_pnl} currency="SOL" digits={6} />
                  </td>
                  <td className="muted">
                    SL {p.stop_loss ?? "—"} · TP {(p.take_profits ?? []).map((tp, i) => `${(p.tp_hits ?? []).includes(i) ? "✓" : ""}${tp}`).join(", ") || "—"}
                    {p.trailing_stop ? ` · trail ${p.trailing_stop}` : ""}
                  </td>
                  <td>
                    {formatDate(p.entry_at)}
                    {p.status === "open" && <div className="muted">age {fmtDuration((Date.now() - new Date(p.entry_at).getTime()) / 1000)}</div>}
                    <Sig sig={p.entry_signature} />
                  </td>
                  <td>
                    {p.status === "open" && !p.exit_requested ? (
                      <SellButton positionId={p.id} symbol={p.symbol} mode="LIVE" route={p.route} />
                    ) : p.exit_requested ? <span className="pill pill-warn">sell requested</span> : null}
                  </td>
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
