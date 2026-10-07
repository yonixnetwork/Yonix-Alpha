"use client";

import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";
import Link from "next/link";
import MarketCap from "@/components/MarketCap";
import { apiGet, apiPost, ApiError } from "@/lib/api";
import { formatDate, formatDecimal } from "@/lib/format";

interface Preview {
  mint: string; symbol: string | null; name: string | null; engine: string; route: string; migration_state: string;
  current_price_sol: string | null; price_source: string; price_at: string | null; execution_mode: "LIVE" | "PAPER";
  market_cap_sol?: string | null; market_cap_basis?: string; risk_status?: string | null;
  liquidity?: { sol: string; kind: string; at: string | null } | null;
  global_mode: string; slippage_limit_pct: string; estimated_quantity: string | null; note: string;
  balance: { kind: string; sol: string | null; available_sol: string | null; at: string | null; live_ready?: boolean; live_not_ready_reason?: string | null };
  latest_assessment: null | { evaluated_at: string; age_seconds: number; decision: string; status: string; overall_risk: string;
    planned_size_sol: string | null; max_loss_sol: string | null; stop_loss: string | null; safety_blockers: string[] };
}
interface Req {
  id: string; status: string; route: string; engine: string; stage?: string; reason?: string; decision?: string; target?: string;
  size?: string | null; position_id?: string; failure_stage?: string | null;
  blockers?: { code: string; category: string; message: string }[];
  data_errors?: string[];
  history: { status: string; at: string; reason?: string }[];
  order?: { signature: string | null; status: string; error: string | null } | null;
}

const FINAL = new Set(["BLOCKED", "PAPER_POSITION_OPEN", "POSITION_OPEN", "FAILED", "EXPIRED", "CONFIRMED"]);
const STATUS_TEXT: Record<string, string> = {
  QUEUED: "QUEUED — waiting for the decision engine", EVALUATING: "EVALUATING — running the full safety gate",
  SUBMITTING: "SUBMITTING", CONFIRMING: "CONFIRMING", CONFIRMED: "CONFIRMED", POSITION_OPEN: "POSITION OPEN",
  PAPER_POSITION_OPEN: "PAPER POSITION OPEN (simulated)", BLOCKED: "BLOCKED BY SAFETY", FAILED: "FAILED",
};
function statusPill(s: string) {
  return s.includes("OPEN") || s === "CONFIRMED" ? "pill pill-ok" : s === "BLOCKED" || s === "FAILED" ? "pill pill-danger" : "pill pill-warn";
}

/** BUY on a token: Confirm Purchase dialog (preview) → the full safety gate
 * runs in the decision engine → live status. The button never bypasses a
 * safety check; a blocked buy shows the exact reasons. */
export function BuyButton({ mint, engine, source, label = "BUY" }: { mint: string; engine?: string; source: string; label?: string }) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const [preview, setPreview] = useState<Preview | null>(null);
  const [req, setReq] = useState<Req | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const poll = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => { if (poll.current) clearTimeout(poll.current); }, []);

  async function open() {
    setPreview(null); setReq(null); setErr(null);
    ref.current?.showModal();
    try {
      setPreview(await apiGet<Preview>("/api/trade/preview", { mint, engine }));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Preview failed.");
    }
  }

  async function track(id: string) {
    try {
      const r = await apiGet<Req>(`/api/trade/requests/${id}`);
      setReq(r);
      if (!FINAL.has(r.status)) poll.current = setTimeout(() => void track(id), 1500);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Status unavailable.");
    }
  }

  async function confirm() {
    setBusy(true); setErr(null);
    try {
      const r = await apiPost<Req>("/api/trade/buy", { mint, engine, source, confirm: true });
      setReq(r);
      void track(r.id);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Buy request failed.");
    } finally {
      setBusy(false);
    }
  }

  const p = preview;
  const a = p?.latest_assessment;
  return (
    <>
      <button type="button" className="btn btn-sm" onClick={open} aria-label={`Buy ${mint}`}>{label}</button>
      <dialog ref={ref} className="dialog" aria-labelledby={titleId} onClose={() => { if (poll.current) clearTimeout(poll.current); }}>
        <h2 id={titleId} className="dialog-title">{req ? "Buy status" : "Confirm Purchase"}</h2>
        <div className="dialog-body">
          {err && <div className="error" role="alert">{err}</div>}
          {!p && !err && <div className="muted">Loading…</div>}
          {p && !req && (
            <>
              <table className="data-table"><tbody>
                <tr><td>Token</td><td><b>{p.symbol ?? "—"}</b> {p.name ? `(${p.name})` : ""}<div className="mono muted">{p.mint}</div></td></tr>
                <tr><td>Execution</td><td><span className={p.execution_mode === "LIVE" ? "pill pill-danger" : "pill pill-off"}>{p.execution_mode}</span>
                  {p.execution_mode === "LIVE" ? " — real SOL from the live wallet" : " — simulated (global mode " + p.global_mode + ")"}
                  {p.balance.live_ready === false && <div className="neg">live not ready: {p.balance.live_not_ready_reason}</div>}</td></tr>
                <tr><td>Route</td><td>{p.route} <span className="muted">· {p.migration_state}</span></td></tr>
                <tr><td>Current price</td><td>{p.current_price_sol ? `${formatDecimal(p.current_price_sol, 12)} SOL` : "—"} <span className="muted">{p.price_source}{p.price_at ? ` · ${formatDate(p.price_at)}` : ""}</span></td></tr>
                <tr><td>Market cap</td><td><MarketCap sol={p.market_cap_sol} /> <span className="muted">{p.market_cap_basis ?? ""}</span></td></tr>
                <tr><td>Liquidity</td><td>{p.liquidity ? `${formatDecimal(p.liquidity.sol, 4)} SOL` : "—"} <span className="muted">{p.liquidity ? `${p.liquidity.kind}${p.liquidity.at ? ` · ${formatDate(p.liquidity.at)}` : ""}` : "not observed"}</span></td></tr>
                <tr><td>Available balance</td><td>{p.balance.available_sol ? `${formatDecimal(p.balance.available_sol, 6)} SOL` : "unknown"} <span className="muted">{p.balance.kind}</span></td></tr>
                <tr><td>Estimated amount</td><td>{a?.planned_size_sol ? `${formatDecimal(a.planned_size_sol, 6)} SOL` : "set by the gate at execution"}
                  {p.estimated_quantity && <span className="muted"> ≈ {formatDecimal(p.estimated_quantity, 2)} tokens</span>}</td></tr>
                <tr><td>Slippage limit</td><td>{p.slippage_limit_pct}%</td></tr>
                <tr><td>Risk status</td><td>{a ? <>{a.status} · risk {a.overall_risk} <span className="muted">({a.age_seconds}s ago)</span></> : "not assessed yet"}
                  {a && a.safety_blockers.length > 0 && <ul className="reason-list">{a.safety_blockers.map((b) => <li key={b}>{b}</li>)}</ul>}</td></tr>
                <tr><td>Maximum potential loss</td><td>{a?.max_loss_sol ? `${formatDecimal(a.max_loss_sol, 6)} SOL (stop at ${a.stop_loss ? formatDecimal(a.stop_loss, 12) : "—"} SOL)` : "calculated by the gate"}</td></tr>
              </tbody></table>
              <p className="muted">{p.note}</p>
            </>
          )}
          {req && (
            <div aria-live="polite">
              <div><span className={statusPill(req.status)}>{STATUS_TEXT[req.status] ?? req.status}</span>
                {req.failure_stage && <span className="pill pill-danger"> {req.failure_stage}</span>}</div>
              <div className="muted">route {req.route}{req.target ? ` · ${req.target}` : ""}{req.size ? ` · size ${formatDecimal(req.size, 6)} SOL` : ""}</div>
              {req.reason && <div className={req.status === "BLOCKED" || req.status === "FAILED" ? "neg" : "muted"}>{req.reason}</div>}
              {req.blockers && req.blockers.length > 0 && (
                <ul className="reason-list">{req.blockers.map((b, i) => <li key={i}><b>{b.code}</b> ({b.category}): {b.message}</li>)}</ul>)}
              {req.data_errors && req.data_errors.length > 0 && (
                <div className="notice">Why data was missing:
                  <ul className="reason-list">{req.data_errors.map((e, i) => <li key={i} className="mono">{e}</li>)}</ul>
                  {req.data_errors.some((e) => /RPC endpoints failed|429/.test(e)) && (
                    <div>The RPC provider refused or failed these requests. Add or enable a backup under{" "}
                      <Link href="/dashboard/rpc">System → RPC &amp; Data Providers</Link>.</div>)}
                </div>)}
              {req.order?.signature && <div className="mono muted">tx {req.order.signature}</div>}
              <ol className="muted">{req.history.map((h, i) => <li key={i}>{h.status} · {formatDate(h.at)}</li>)}</ol>
              {req.position_id && <Link href={`/dashboard/trades/${req.position_id}`}>open position</Link>}
            </div>
          )}
        </div>
        <div className="btn-row dialog-actions">
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => ref.current?.close()}>{req ? "CLOSE" : "CANCEL"}</button>
          {!req && (
            <button type="button" className={p?.execution_mode === "LIVE" ? "btn btn-danger btn-sm" : "btn btn-sm"} disabled={!p || busy} onClick={confirm}>
              {busy ? "Submitting…" : "CONFIRM BUY"}
            </button>
          )}
        </div>
      </dialog>
    </>
  );
}

interface OrderView { side: string; status: string; signature: string | null; error: string | null; failure_stage?: string;
  order_submitted: boolean; transaction_confirmed: boolean; actually_filled: boolean; created_at: string }

/** SELL the whole open position through the normal exit path (current
 * route, slippage limit, transaction guard), even when no automatic exit
 * is triggering. Status follows the real SELL order. */
export function SellButton({ positionId, symbol, mode, route, description }: {
  positionId: string; symbol: string | null; mode: string; route: string | null;
  /** Replaces the Solana route text (e.g. for an EVM paper position). */
  description?: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const [state, setState] = useState<"confirm" | "tracking">("confirm");
  const [orders, setOrders] = useState<OrderView[]>([]);
  const [pos, setPos] = useState<{ status: string; exit_reason: string | null } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const poll = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (poll.current) clearTimeout(poll.current); }, []);

  async function track() {
    try {
      const r = await apiGet<{ position: { status: string; exit_reason: string | null }; orders: OrderView[] }>(`/api/trade/positions/${positionId}`);
      setPos(r.position);
      setOrders(r.orders.filter((o) => o.side === "SELL"));
      if (r.position.status === "open") poll.current = setTimeout(() => void track(), 2000);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Status unavailable.");
    }
  }

  async function confirm() {
    setBusy(true); setErr(null);
    try {
      await apiPost(`/api/trade/sell/${positionId}?confirm=true`);
      setState("tracking");
      void track();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Sell request failed.");
    } finally {
      setBusy(false);
    }
  }

  const last = orders[orders.length - 1];
  const stage = !last ? (pos?.status === "closed" ? "POSITION CLOSED" : "SELL REQUESTED — waiting for the position manager")
    : last.failure_stage ? last.failure_stage : last.transaction_confirmed ? "CONFIRMED" : last.order_submitted ? "CONFIRMING" : "SUBMITTING";
  return (
    <>
      <button type="button" className="btn btn-danger btn-sm" onClick={() => { setState("confirm"); setErr(null); ref.current?.showModal(); }}>SELL</button>
      <dialog ref={ref} className="dialog" aria-labelledby={titleId} onClose={() => { if (poll.current) clearTimeout(poll.current); }}>
        <h2 id={titleId} className="dialog-title">{state === "confirm" ? "Confirm Sell" : "Sell status"}</h2>
        <div className="dialog-body">
          {err && <div className="error" role="alert">{err}</div>}
          {state === "confirm" ? (description ? <p>{description}</p> :
            <p>Sell the whole remaining <b>{symbol ?? "position"}</b> ({mode}) through route <b>{route ?? "—"}</b>. The route is the
              position&apos;s current one (switched to PumpSwap automatically after a migration); the exit slippage limit and the
              transaction guard apply.</p>
          ) : (
            <div aria-live="polite">
              <span className={stage.includes("FAIL") ? "pill pill-danger" : stage === "CONFIRMED" || stage === "POSITION CLOSED" ? "pill pill-ok" : "pill pill-warn"}>{stage}</span>
              {pos?.status === "closed" && <div className="muted">position closed{pos.exit_reason ? `: ${pos.exit_reason}` : ""}</div>}
              {last?.signature && <div className="mono muted">tx {last.signature}</div>}
              {last?.error && <div className="neg">{last.error}</div>}
            </div>
          )}
        </div>
        <div className="btn-row dialog-actions">
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => ref.current?.close()}>{state === "confirm" ? "CANCEL" : "CLOSE"}</button>
          {state === "confirm" && <button type="button" className="btn btn-danger btn-sm" disabled={busy} onClick={confirm}>{busy ? "Submitting…" : "CONFIRM SELL"}</button>}
        </div>
      </dialog>
    </>
  );
}

/** Closes a LIVE position whose tokens were already sold or moved outside
 * this system (for example in a wallet app). The server reads the wallet on
 * chain first and refuses while the wallet still holds the token; the
 * realized PnL stays unknown. `positionId` omitted: every open or
 * needs_review LIVE position whose token the wallet no longer holds. */
export function CloseOutsideButton({ positionId, symbol, onDone }: {
  positionId?: string; symbol?: string | null; onDone?: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  async function confirm() {
    setBusy(true); setErr(null);
    try {
      if (positionId) {
        await apiPost(`/api/trade/close-outside/${positionId}?confirm=true`);
        setResult("Position closed as sold outside the system.");
      } else {
        const r = await apiPost<{ closed: { symbol: string }[]; kept: { symbol: string; why: string }[] }>(
          "/api/trade/close-outside-all", { confirm: "CLOSE SOLD OUTSIDE" });
        setResult(`Closed ${r.closed.length}: ${r.closed.map((c) => c.symbol).join(", ") || "none"}. ` +
          (r.kept.length ? `Left open ${r.kept.length}: ${r.kept.map((k) => `${k.symbol} (${k.why})`).join("; ")}` : ""));
      }
      onDone?.();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Request failed.");
    } finally {
      setBusy(false);
    }
  }

  const label = positionId ? "SOLD OUTSIDE" : "CLOSE ALL SOLD OUTSIDE";
  return (
    <>
      <button type="button" className="btn btn-ghost btn-sm"
        onClick={() => { setErr(null); setResult(null); ref.current?.showModal(); }}>{label}</button>
      <dialog ref={ref} className="dialog" aria-labelledby={titleId}>
        <h2 id={titleId} className="dialog-title">Close as sold outside the system</h2>
        <div className="dialog-body">
          {err && <div className="error" role="alert">{err}</div>}
          {result ? <p aria-live="polite">{result}</p> : (
            <p>
              {positionId ? <>Close <b>{symbol ?? "this position"}</b></> : <>Close every open or needs-review LIVE position</>}{" "}
              whose token the wallet no longer holds (sold or moved in a wallet app). The wallet is checked on chain first;
              a token still in the wallet is not closed. No sell is sent. The exit price and realized PnL are recorded as
              unknown.
            </p>
          )}
        </div>
        <div className="btn-row dialog-actions">
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => ref.current?.close()}>{result ? "CLOSE" : "CANCEL"}</button>
          {!result && <button type="button" className="btn btn-danger btn-sm" disabled={busy} onClick={confirm}>{busy ? "Checking wallet…" : "CONFIRM"}</button>}
        </div>
      </dialog>
    </>
  );
}
