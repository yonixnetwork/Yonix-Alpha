"use client";

import { useState } from "react";
import { ErrorNotice, Loading, Section, Stat } from "@/components/ui";
import { apiPost, ApiError } from "@/lib/api";
import { formatDate, formatDecimal } from "@/lib/format";
import { useApi } from "@/lib/useApi";

interface OrderView {
  side: string; reason: string; route: string; provider: string; status: string; signature: string | null;
  requested: { amount: string; amount_kind: string; slippage_pct: string };
  created_at: string; submitted_at: string | null; confirmed_at: string | null;
  order_submitted: boolean; transaction_confirmed: boolean; actually_filled: boolean; error: string | null;
  failure_stage?: string;
  actual?: { tokens: string; sol: string; network_fee_sol: string; price_sol: string | null; slippage_vs_plan_pct?: string };
}
interface RunView {
  id: string; category: string; engine: string; max_sol: string; status: string; stage: string | null;
  stage_reason: string | null; armed_by: string; created_at: string; expires_at: string; mint: string | null;
  attempts: { at: string; mint: string; symbol: string; stage: string; codes: string[]; reason: string }[];
  buy: OrderView | null; sells: OrderView[];
  position: { id: string; status: string; entry_price: string | null; current_price: string | null; price_status: string;
    price_at: string | null; unrealized_pnl: string | null; unrealized_pnl_pct: string | null; realized_pnl: string | null;
    execution_route: string | null; exit_requested: boolean } | null;
}
interface Smoke {
  config: { enabled: boolean; max_sol: string | null; max_trades: number; problems: string[]; confirm_phrase: string; categories: string[] };
  trades_used: number; armed: string | null; runs: RunView[];
}

function Flag({ ok, label }: { ok: boolean; label: string }) {
  return <span className={ok ? "pill pill-ok" : "pill pill-off"}>{label}: {ok ? "YES" : "NO"}</span>;
}

function OrderLine({ o }: { o: OrderView }) {
  return (
    <div className="card">
      <b>{o.side}</b> {o.requested.amount} {o.requested.amount_kind} · slippage limit {o.requested.slippage_pct}% · {o.route} via {o.provider}
      <div style={{ marginTop: 4 }}>
        <Flag ok={o.order_submitted} label="ORDER_SUBMITTED" /> <Flag ok={o.transaction_confirmed} label="TRANSACTION_CONFIRMED" />{" "}
        <Flag ok={o.actually_filled} label="ACTUALLY_FILLED" />
        {o.failure_stage && <span className="pill pill-danger"> {o.failure_stage}</span>}
      </div>
      <div className="muted">
        {o.signature ? <>tx <code>{o.signature}</code> · </> : null}
        created {formatDate(o.created_at)} · submitted {formatDate(o.submitted_at)} · confirmed {formatDate(o.confirmed_at)}
      </div>
      {o.actual && (
        <div className="muted">
          filled {formatDecimal(o.actual.tokens, 2)} tokens for {formatDecimal(o.actual.sol, 6)} SOL · price {o.actual.price_sol ?? "—"} ·
          network fee {o.actual.network_fee_sol} SOL{o.actual.slippage_vs_plan_pct ? ` · slippage vs plan ${o.actual.slippage_vs_plan_pct}%` : ""}
        </div>
      )}
      {o.error && <div className="neg">{o.error}</div>}
    </div>
  );
}

/** LIVE_EXECUTION_SMOKE_TEST: arm one tiny, fully gated live buy to verify
 * the real execution path. Off unless enabled in the server's .env. */
export default function SmokeTestPanel() {
  const { data, error, reload } = useApi<Smoke>("/api/live/smoke-test", undefined, {
    refreshMs: 5000, reloadOn: ["trade.created", "trade.updated", "trade.closed", "balance.updated"],
  });
  const [category, setCategory] = useState("FRESH");
  const [maxSol, setMaxSol] = useState("");
  const [minutes, setMinutes] = useState("30");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function act(path: string, body?: unknown) {
    setBusy(true);
    setMsg(null);
    try {
      await apiPost(path, body);
      setPassword("");
      setConfirm("");
      await reload();
    } catch (err) {
      setMsg(err instanceof ApiError ? err.message : "Request failed.");
    } finally {
      setBusy(false);
    }
  }

  if (!data) {
    return (
      <Section title="Live execution smoke test — verification with real SOL">
        {error ? <ErrorNotice error={error} /> : <Loading what="Loading smoke test status…" />}
      </Section>
    );
  }
  const c = data.config;
  const blocked = c.problems.length > 0;
  return (
    <Section title="Live execution smoke test — verification with real SOL">
      <div className="card warn-card">
        This buys a real token with real SOL (at most {c.max_sol ?? "—"} SOL, {data.trades_used}/{c.max_trades} test buys used).
        The token is chosen only if it passes every safety check against the live wallet; if none does, nothing is bought
        (NO_TEST_EXECUTION_CANDIDATE). The global mode stays as it is. It is an execution check, not a strategy.
      </div>
      {blocked && (
        <>
          <div style={{ marginTop: 8 }}>
            <span className="pill pill-warn">BLOCKED</span> The server does not allow arming a test buy until all of these are
            fixed in its .env (then redeploy):
          </div>
          <ul className="reason-list">{c.problems.map((p) => <li key={p}>{p}</li>)}</ul>
        </>
      )}
      {!data.armed && !blocked && data.trades_used < c.max_trades && (
        <form className="form-grid" onSubmit={(e) => { e.preventDefault(); act("/api/live/smoke-test/arm", {
          category, max_sol: maxSol || undefined, minutes: Number(minutes), password, confirm }); }}>
          <label>Category
            <select value={category} onChange={(e) => setCategory(e.target.value)}>
              {c.categories.map((x) => <option key={x} value={x}>{x}</option>)}
            </select>
          </label>
          <label>Max SOL (≤ {c.max_sol})<input inputMode="decimal" value={maxSol} placeholder={c.max_sol ?? ""} onChange={(e) => setMaxSol(e.target.value)} /></label>
          <label>Expires after (minutes)<input inputMode="numeric" value={minutes} onChange={(e) => setMinutes(e.target.value)} /></label>
          <label>Admin password<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
          <label>Type <code>{c.confirm_phrase}</code><input value={confirm} onChange={(e) => setConfirm(e.target.value)} /></label>
          <button className="btn btn-danger" type="submit" disabled={busy || !password || confirm !== c.confirm_phrase}>Arm one test buy</button>
        </form>
      )}
      {msg && <div className="error" role="alert">{msg}</div>}
      {data.runs.map((r) => (
        <div key={r.id} className="card">
          <div className="stat-grid">
            <Stat label="Run">{r.category} · {r.status}</Stat>
            <Stat label="Stage">{r.stage ?? "—"}</Stat>
            <Stat label="Max SOL">{r.max_sol}</Stat>
            <Stat label="Armed">{formatDate(r.created_at)} by {r.armed_by}</Stat>
            <Stat label="Expires">{formatDate(r.expires_at)}</Stat>
          </div>
          {r.stage_reason && <div className="muted">{r.stage_reason}</div>}
          <div className="btn-row">
            {r.status === "ARMED" && <button className="btn" disabled={busy} onClick={() => act(`/api/live/smoke-test/${r.id}/cancel`)}>Disarm</button>}
            {r.position?.status === "open" && !r.position.exit_requested && (
              <button className="btn btn-danger" disabled={busy} onClick={() => act(`/api/live/smoke-test/${r.id}/close`)}>
                Close test position (sell via the normal exit path)
              </button>
            )}
          </div>
          {r.buy && <OrderLine o={r.buy} />}
          {r.position && (
            <div className="stat-grid">
              <Stat label="Position">{r.position.status}{r.position.exit_requested ? " · exit requested" : ""}</Stat>
              <Stat label="Entry price">{r.position.entry_price ?? "—"}</Stat>
              <Stat label="Current price" hint={`${r.position.price_status} · ${formatDate(r.position.price_at)}`}>
                {r.position.current_price ?? "—"} {r.position.price_status !== "LIVE" && <span className="pill pill-warn">{r.position.price_status}</span>}
              </Stat>
              <Stat label="Unrealized PnL">{r.position.unrealized_pnl ?? "—"} SOL ({r.position.unrealized_pnl_pct ?? "—"}%)</Stat>
              <Stat label="Realized PnL">{r.position.realized_pnl ?? "—"} SOL</Stat>
              <Stat label="Route">{r.position.execution_route ?? "—"}</Stat>
            </div>
          )}
          {r.sells.map((o, i) => <OrderLine key={i} o={o} />)}
          {r.attempts.length > 0 && (
            <table className="data-table">
              <thead><tr><th>Candidate</th><th>Stage</th><th>Codes</th><th>At</th></tr></thead>
              <tbody>{r.attempts.map((a, i) => (
                <tr key={i}><td><code>{a.symbol ?? a.mint.slice(0, 6)}</code></td><td>{a.stage}</td>
                  <td className="muted">{a.codes.join(", ") || a.reason}</td><td>{formatDate(a.at)}</td></tr>))}</tbody>
            </table>
          )}
        </div>
      ))}
    </Section>
  );
}
