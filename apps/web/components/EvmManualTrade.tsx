"use client";

import { useEffect, useId, useRef, useState } from "react";
import { apiGet, apiPost, ApiError } from "@/lib/api";
import { formatDate, formatUsdCompact } from "@/lib/format";

type J = Record<string, any>;
const FINAL = new Set(["BLOCKED", "PAPER_POSITION_OPEN", "FAILED", "EXPIRED"]);
const STATUS_TEXT: Record<string, string> = {
  QUEUED: "QUEUED - waiting for the data-evm worker", EVALUATING: "EVALUATING - running every entry check",
  BLOCKED: "BLOCKED", PAPER_POSITION_OPEN: "PAPER POSITION OPEN (simulated)", FAILED: "FAILED", EXPIRED: "EXPIRED",
};

function pill(s: string) {
  return s === "PAPER_POSITION_OPEN" ? "pill pill-ok" : s === "BLOCKED" || s === "FAILED" || s === "EXPIRED" ? "pill pill-danger" : "pill pill-warn";
}

/** Manual BUY on BSC / Robinhood Chain (master §45): confirmation dialog
 * with the preview, then the request status. The data-evm worker runs the
 * same entry checks as automatic trading (only the trade signal is replaced
 * by the operator's decision); a blocked buy lists every reason. Paper only:
 * EVM live execution is locked. No override control exists. */
export function EvmBuyButton({ chain, token, disabledReason }: { chain: string; token: string; disabledReason?: string | null }) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const [preview, setPreview] = useState<J | null>(null);
  const [req, setReq] = useState<J | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const poll = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (poll.current) clearTimeout(poll.current); }, []);

  async function open() {
    setPreview(null); setReq(null); setErr(null);
    ref.current?.showModal();
    try {
      setPreview(await apiGet<J>("/api/trade/evm/preview", { chain, token }));
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Preview failed.");
    }
  }

  async function track(id: string) {
    try {
      const r = await apiGet<J>(`/api/trade/evm/requests/${id}`);
      setReq(r);
      if (!FINAL.has(r.status)) poll.current = setTimeout(() => void track(id), 1500);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Status unavailable.");
    }
  }

  async function confirm() {
    setBusy(true); setErr(null);
    try {
      const r = await apiPost<J>("/api/trade/evm/buy", { chain, token, confirm: true });
      setReq(r);
      void track(r.id);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Buy request failed.");
    } finally {
      setBusy(false);
    }
  }

  const p = preview;
  const usd = p?.native_usd?.price ? Number(p.native_usd.price) : null;
  return (
    <>
      <button type="button" className="btn btn-sm" disabled={!!disabledReason} title={disabledReason ?? "Paper BUY through every entry check"} onClick={open}>
        BUY (paper)
      </button>
      <dialog ref={ref} className="dialog" aria-labelledby={titleId} onClose={() => { if (poll.current) clearTimeout(poll.current); }}>
        <h2 id={titleId} className="dialog-title">{req ? "Buy status" : "Confirm Purchase"}</h2>
        <div className="dialog-body">
          {err && <div className="error" role="alert">{err}</div>}
          {!req && !p && !err && <p className="muted">Loading preview...</p>}
          {!req && p && (
            <>
              <dl className="term-kv small">
                <dt>Token</dt><dd>{p.symbol ?? "—"} <span className="muted">{p.name ?? ""}</span><div className="mono muted">{p.token}</div></dd>
                <dt>Chain / launchpad</dt><dd>{chain === "bsc" ? "BSC" : "Robinhood Chain"} · {p.launchpad} {p.observe_only && <span className="pill pill-danger">observe only</span>}</dd>
                <dt>Launchpad status</dt><dd>{p.launchpad_status.status} {p.launchpad_status.paper_allowed ? "" : <span className="neg">(paper trading not allowed: {p.launchpad_status.why})</span>}</dd>
                <dt>Route</dt><dd>{p.route} · {p.category} · {p.stage}</dd>
                <dt>Price</dt><dd className="mono">{p.price_native ?? "—"} {p.currency}{usd && p.price_native ? <span className="muted"> (${(Number(p.price_native) * usd).toPrecision(4)})</span> : null}</dd>
                <dt>Liquidity</dt><dd>{p.liquidity_native ? `${Number(p.liquidity_native).toFixed(3)} ${p.currency}` : "—"}{usd && p.liquidity_native ? <span className="muted"> ({formatUsdCompact(Number(p.liquidity_native) * usd)})</span> : null}</dd>
                <dt>Size</dt><dd>{p.size} {p.currency} <span className="muted">(gas reserve {p.gas_reserve} {p.currency} held back)</span></dd>
                <dt>Paper balance</dt><dd>{Number(p.balance.cash).toFixed(4)} {p.currency}</dd>
                <dt>Safety</dt><dd>{p.safety.verdict ?? "not checked"} {p.safety.age_seconds != null && <span className="muted">({Math.round(p.safety.age_seconds)}s old; re-run when older than 5 minutes)</span>}
                  {p.safety.blocking.length > 0 && <ul className="small neg">{p.safety.blocking.map((b: string) => <li key={b}>{b}</li>)}</ul>}</dd>
                <dt>Mode</dt><dd><span className="pill pill-off">PAPER</span></dd>
              </dl>
              <p className="form-hint">{p.note}</p>
            </>
          )}
          {req && (
            <div aria-live="polite">
              <span className={pill(req.status)}>{STATUS_TEXT[req.status] ?? req.status}</span>
              {req.status === "BLOCKED" && (
                <ul className="small">{(req.blockers ?? []).map((b: J, i: number) => <li key={i}><b>{b.code}</b>: {b.message}</li>)}</ul>
              )}
              {req.error && <div className="neg small">{req.error}</div>}
              {req.position_id && <p className="small">Position <a className="link" href={`/dashboard/trades/${req.position_id}`}>{req.position_id.slice(0, 8)}</a> opened.</p>}
              <ol className="muted small">{(req.history ?? []).map((h: J, i: number) => <li key={i}>{h.status} · {formatDate(h.at)}</li>)}</ol>
            </div>
          )}
        </div>
        <div className="btn-row dialog-actions">
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => ref.current?.close()}>{req ? "CLOSE" : "CANCEL"}</button>
          {!req && <button type="button" className="btn btn-sm" disabled={!p || busy || p.observe_only} onClick={confirm}>{busy ? "Submitting..." : "CONFIRM BUY"}</button>}
        </div>
      </dialog>
    </>
  );
}
