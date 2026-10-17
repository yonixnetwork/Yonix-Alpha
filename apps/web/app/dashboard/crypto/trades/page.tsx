"use client";

// Crypto Trading - Exchange Trades. Trades on exchange accounts (practice
// demo / testnet now; live only after live trading is reviewed and unlocked
// in code). Every value comes from the backend: the exchange-confirmed
// state, the protection status and each recorded management decision.

import { Fragment, useState } from "react";
import { apiPost, ApiError } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type Trade = {
  id: string; connection: string; exchange: string; environment: string; strategy_id: string; symbol: string;
  status: string; protection: string; qty_planned: string; qty: string; entry_price: string | null;
  initial_stop: string; stop: string; take_profit: string | null; risk_budget: string; planned_loss: string;
  plan: { engine_take_profit?: boolean; trailing?: { method: string } }; realized_quote: string;
  close_reason: string | null; created_at: string; closed_at: string | null;
  last_action: { action: string; reason: string; at: string } | null;
};
type Guard = { connection: string; exchange: string; environment: string; entries_blocked: boolean; reason: string | null; since: string | null };
type Detail = { actions: { at: string; action: string; reason: string }[];
  orders: { client_id: string; role: string; kind: string; status: string; qty: string; filled_qty: string; avg_price: string | null; trigger_price: string | null; price: string | null }[] };

function errText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  return e instanceof Error ? e.message : String(e);
}

const n = (v: string | null | undefined, d = 6) => (v == null ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d }));

function DetailRow({ id }: { id: string }) {
  const { data, error } = useApi<Detail>(`/api/crypto/exec/trades/${id}`);
  if (error) return <tr><td colSpan={9} className="error">{error}</td></tr>;
  if (!data) return <tr><td colSpan={9} className="muted">Loading…</td></tr>;
  return (
    <tr>
      <td colSpan={9}>
        <div className="small" style={{ display: "grid", gap: 8 }}>
          <div><strong>Orders</strong> (as last confirmed by the exchange)
            {data.orders.map((o) => (
              <div key={o.client_id} className="mono">{o.role} {o.kind} {o.status} · qty {n(o.qty)} filled {n(o.filled_qty)}
                {o.avg_price ? ` @ ${n(o.avg_price, 4)}` : ""}{o.trigger_price ? ` · trigger ${n(o.trigger_price, 4)}` : ""} · {o.client_id}</div>
            ))}
          </div>
          <div><strong>Decisions and actions</strong>
            {data.actions.map((a, i) => (
              <div key={i}><span className="muted">{formatDate(a.at)}</span> <span className="pill pill-off">{a.action}</span> {a.reason}</div>
            ))}
          </div>
        </div>
      </td>
    </tr>
  );
}

export default function ExchangeTrades() {
  const trades = useApi<Trade[]>("/api/crypto/exec/trades", { limit: 50 }, { refreshMs: 15000 });
  const guards = useApi<Guard[]>("/api/crypto/exec/guards", undefined, { refreshMs: 15000 });
  const [open, setOpen] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  async function close(t: Trade) {
    const password = window.prompt(`Close the ${t.environment} ${t.symbol} trade now? The stop is cancelled (confirmed) and the position is sold at market.\n\nEnter your dashboard password:`);
    if (!password) return;
    try {
      const r = await apiPost<{ status: string; close_reason: string | null }>(`/api/crypto/exec/trades/${t.id}/close`, { password });
      setMsg(`Trade ${r.status}${r.close_reason ? `: ${r.close_reason}` : ""}`);
      trades.reload();
    } catch (e) {
      setMsg(errText(e));
    }
  }

  const blocked = (guards.data ?? []).filter((g) => g.entries_blocked);
  return (
    <div>
      <div className="page-header">
        <div className="page-title">Crypto Trading · Exchange Trades</div>
      </div>
      <div className="notice">
        Trades on exchange accounts. A stop counts as in place only when the exchange reports it open. Uncertain state
        (an order with no answer, a missing stop, coins missing from the account) blocks new entries until a
        reconciliation finds everything consistent. Practice (demo / testnet) only: live orders are locked.
      </div>
      {blocked.map((g) => (
        <div key={g.connection} className="notice notice-danger">
          New entries blocked on {g.exchange} {g.environment} &quot;{g.connection}&quot; since {formatDate(g.since)}: {g.reason}
        </div>
      ))}
      {msg && <div className="notice notice-warn">{msg}</div>}
      {trades.error && <div className="error">{trades.error}</div>}
      {trades.data && trades.data.length === 0 && <div className="empty-state">No exchange trade yet.</div>}
      {trades.data && trades.data.length > 0 && (
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Trade</th><th>Status</th><th>Protection</th><th>Held</th><th>Entry</th><th>Stop</th><th>Target</th><th>Realized</th><th>Latest decision</th></tr></thead>
            <tbody>
              {trades.data.map((t) => (
                <Fragment key={t.id}>
                  <tr className="clickable" onClick={() => setOpen(open === t.id ? null : t.id)}>
                    <td>{t.symbol} <span className={`pill ${t.environment === "LIVE" ? "pill-danger" : "pill-warn"}`}>{t.environment}</span>
                      <div className="muted small">{t.exchange} &quot;{t.connection}&quot; · {t.strategy_id} · {formatDate(t.created_at)}</div></td>
                    <td><span className={`pill ${t.status === "OPEN" ? "pill-ok" : ["UNCERTAIN", "FAILED"].includes(t.status) ? "pill-danger" : "pill-off"}`}>{t.status}</span>
                      {["OPEN", "UNCERTAIN"].includes(t.status) && (
                        <div><button className="btn btn-sm btn-ghost" onClick={(e) => { e.stopPropagation(); close(t); }}>Close</button></div>)}</td>
                    <td><span className={`pill ${t.protection === "VERIFIED" ? "pill-ok" : t.protection === "MISSING" ? "pill-danger" : "pill-off"}`}>{t.protection}</span></td>
                    <td className="mono">{n(t.qty)}<div className="muted small">planned {n(t.qty_planned)}</div></td>
                    <td className="mono">{n(t.entry_price, 4)}</td>
                    <td className="mono">{n(t.stop, 4)}<div className="muted small">initial {n(t.initial_stop, 4)} · risk {n(t.planned_loss, 4)} of {n(t.risk_budget, 4)}</div></td>
                    <td className="mono">{n(t.take_profit, 4)}<div className="muted small">{t.take_profit ? (t.plan.engine_take_profit ? "engine-managed" : "on the exchange") : ""}</div></td>
                    <td className={`mono ${Number(t.realized_quote) > 0 ? "pos" : Number(t.realized_quote) < 0 ? "neg" : ""}`}>{n(t.realized_quote, 4)}</td>
                    <td className="small">{t.last_action ? <><span className="pill pill-off">{t.last_action.action}</span> {t.last_action.reason}</> : "—"}
                      {t.close_reason && <div className="muted">{t.close_reason}</div>}</td>
                  </tr>
                  {open === t.id && <DetailRow id={t.id} />}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
