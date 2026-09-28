"use client";

import type { ExecutionDiagnostics as Diag, ExecutionOrderRow } from "@/lib/cc";
import { formatDecimal } from "@/lib/format";

const STEPS: [keyof NonNullable<Diag["timing"]>, string][] = [
  ["decision_eval_ms", "Decision (data + gate)"],
  ["approval_to_order_ms", "Approval → order"],
  ["queue_wait_ms", "Order queued"],
  ["quote_latency_ms", "Quote (on-chain state)"],
  ["build_ms", "Build (incl. blockhash)"],
  ["guard_and_recheck_ms", "Guard + migration re-check"],
  ["simulation_ms", "Simulation"],
  ["submission_latency_ms", "Submission"],
  ["submit_to_seen_ms", "Submit → first seen"],
  ["submit_to_confirm_ms", "Submit → confirmed"],
];

const ms = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v.toLocaleString()} ms`);
const pct = (v: string | null | undefined) => (v === null || v === undefined ? "—" : `${Number(v) > 0 ? "+" : ""}${v}%`);
const pctClass = (v: string | null | undefined) => (v === null || v === undefined ? "muted" : Number(v) > 0 ? "neg" : Number(v) < 0 ? "pos" : "");

/** Where the time went and why the price differed, per on-chain order:
 * measured stage timestamps and the program's own trade event. */
export default function ExecutionDiagnostics({ order }: { order: ExecutionOrderRow }) {
  const d = order.diagnostics;
  if (!d || (!d.timing && !d.price)) return <div className="muted">No execution diagnostics recorded for this order.</div>;
  const t = d.timing ?? {};
  const worst = Math.max(1, ...STEPS.map(([k]) => Number(t[k] ?? 0)));
  const p = d.price;
  const c = p?.components_pct ?? {};
  return (
    <div className="diag">
      <div className="diag-head">
        <span className="pill pill-off">{order.side} · {order.reason}</span>
        <span>decision → submit <b>{ms(t.decision_to_submit_ms)}</b></span>
        <span>decision → confirmed <b>{ms(t.decision_to_confirm_ms)}</b></span>
        <span className="muted">RPC before submit {ms(t.rpc_latency_before_submit_ms)} over {t.rpc_calls_before_submit ?? "—"} calls</span>
        <span className="muted">landed after {t.slots_to_land ?? "—"} slots</span>
        {d.timing_reconstructed && <span className="muted">(timing reconstructed from recorded stages)</span>}
      </div>
      <div className="latency-bars">
        {STEPS.map(([k, label]) => {
          const v = t[k] as number | null | undefined;
          return (
            <div className="latency-row" key={k}>
              <span className="latency-label">{label}</span>
              <span className="latency-track"><span className="latency-fill" style={{ width: `${v ? Math.max(1, (v / worst) * 100) : 0}%` }} /></span>
              <span className="latency-val mono">{ms(v)}</span>
            </div>
          );
        })}
      </div>
      {p && (
        <div className="diag-price">
          <div className="diag-chain mono">
            <span title="price the decision used">decision {formatDecimal(p.decision_price_sol ?? null, 12)}</span>
            <span className={pctClass(c.decision_to_build_pct)}>{pct(c.decision_to_build_pct)}</span>
            <span title="curve/pool spot when the transaction was built">build {formatDecimal(p.spot_at_build_sol ?? null, 12)}</span>
            <span className={pctClass(c.build_to_landing_pct)}>{pct(c.build_to_landing_pct)}</span>
            <span title="spot right before our trade, from the program's trade event">before ours {formatDecimal(p.spot_before_trade_sol ?? null, 12)}</span>
            <span className={pctClass(c.price_impact_pct)}>{pct(c.price_impact_pct)} impact</span>
            <span title="our trade price, fees excluded">trade {formatDecimal(p.trade_price_sol ?? null, 12)}</span>
            <span className={pctClass(c.fees_pct)}>{pct(c.fees_pct)} fees</span>
            <span title="SOL spent incl. every fee / tokens received">all-in {formatDecimal(p.all_in_price_sol ?? null, 12)}</span>
          </div>
          <div>
            Total vs decision <b className={pctClass(c.total_vs_decision_pct)}>{pct(c.total_vs_decision_pct)}</b>
            {p.classification && <> · cause <span className="pill pill-warn">{p.classification}</span></>}
            {(p.evidence ?? []).length > 0 && <span className="muted"> {p.evidence.join("; ")}</span>}
          </div>
          <div className="muted">SOL per whole token. Network fee {p.network_fee_sol ?? "—"} SOL · priority fee {p.priority_fee_sol ?? "—"} SOL.</div>
        </div>
      )}
    </div>
  );
}
