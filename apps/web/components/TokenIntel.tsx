"use client";

import { Fragment, useState } from "react";
import { Brain, Gauge, Layers, ShieldAlert, Users, Waves } from "lucide-react";
import { PathView, RowAnalysis, type LedgerRow } from "@/components/LedgerReview";
import MarketCap from "@/components/MarketCap";
import { Empty, ErrorNotice, Loading, Section, Stat } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const lvlClass = (l?: string) => (l === "HIGH" ? "pill pill-danger" : l === "MEDIUM" ? "pill pill-warn" : l === "UNKNOWN" || !l ? "pill pill-off" : "pill pill-ok");
const show = (v: unknown, digits = 3) => {
  if (v === null || v === undefined) return "unknown";
  const x = Number(v);
  return Number.isFinite(x) ? String(Number(x.toFixed(digits))) : String(v);
};

/** Launch intelligence for one token: regime, curve, flow, momentum,
 * manipulation, wallet intelligence, shadow scores and every decision with
 * its path. Unknown values say so; nothing is shown as 0 when unmeasured. */
export default function TokenIntel({ mint, liveIntel }: { mint: string; liveIntel?: J | null }) {
  const q = useApi<{ items: LedgerRow[] }>("/api/ml/opportunities", { mint, limit: 20 }, { refreshMs: 30000 });
  const [open, setOpen] = useState<string | null>(null);
  const rows = q.data?.items ?? [];
  const intel: J | null = liveIntel ?? rows.find((r) => r.snapshot?.intel && !r.snapshot.intel.error)?.snapshot?.intel ?? null;
  const shadow = rows.find((r) => r.ml_shadow)?.ml_shadow;
  const reg = intel?.regime ?? {}; const now = intel?.now ?? {}; const man = intel?.manipulation ?? {};
  const w = intel?.wallets ?? {}; const sm = w.smart_money ?? {}; const dc = w.dump_cluster ?? {};
  const mom = intel?.momentum ?? {}; const pm = intel?.post_migration;
  return (
    <>
      <Section title="Launch intelligence">
        {!intel ? <Empty>No intelligence recorded for this token yet (it is recorded when the gate or the observation window decides on it).</Empty> : (
          <>
            <p className="muted small">{intel.stage} · as of {formatDate(intel.as_of)} · {intel.feature_version} · {intel.source}</p>
            <div className="stat-grid">
              <Stat label="Regime" hint="Mayhem curves break constant-product math; BOOST adds post-migration buybacks">
                <Layers size={14} aria-hidden /> {reg.mayhem === true ? "Mayhem" : reg.mayhem === false ? "standard" : "Mayhem flag unknown"}
                <div className="muted small">curve math {reg.curve_math ? (reg.curve_math.valid === true ? "valid" : reg.curve_math.valid === false ? "invalid" : "unverified") : "—"}
                  · {reg.data_regime}{reg.instant_bond ? " · instant bond" : ""}{reg.boost_window ? " · BOOST window" : ""}</div></Stat>
              <Stat label="Curve progress"><Gauge size={14} aria-hidden /> {now.curve_progress !== undefined ? `${(Number(now.curve_progress) * 100).toFixed(1)}%` : "unknown"}
                <div className="muted small">{now.sol_accumulated !== undefined ? `${show(now.sol_accumulated)} SOL accumulated` : now.unknown?.curve_progress ?? ""}</div></Stat>
              <Stat label="Flow state"><Waves size={14} aria-hidden /> {intel.flow_state?.state ?? "unknown"}
                <div className="muted small">{(intel.flow_state?.evidence ?? []).slice(0, 2).join("; ")}</div></Stat>
              <Stat label="Buyer breadth">{show(intel.buyer_breadth?.score, 2)}
                <div className="muted small">{intel.buyer_breadth?.unique_buyers ?? "—"} buyers / {intel.buyer_breadth?.buys ?? "—"} buys (last minute)</div></Stat>
              <Stat label="Momentum (1 m / 5 m)">{show(mom.return_1m)} / {show(mom.return_5m)}
                <div className="muted small">buyers ×{show(mom.buyer_acceleration, 2)} · trades ×{show(mom.trade_rate_acceleration, 2)}</div></Stat>
              <Stat label="Manipulation" hint="independent families of evidence; patterns, not proof">
                <ShieldAlert size={14} aria-hidden /> <span className={lvlClass(man.level)}>{man.level ?? "UNKNOWN"}</span>
                <div className="muted small">{Object.keys(man.families ?? {}).join(", ") || (man.unknown ?? []).join("; ")}</div></Stat>
              <Stat label="Wallet intelligence" hint="Beta-shrunk reputation from resolved launches only; never a BUY trigger">
                <Users size={14} aria-hidden /> smart money {sm.status === "MEASURED" ? `${sm.proven_wallets} proven` : "unknown"}
                <div className="muted small">dump cluster <span className={lvlClass(dc.level)}>{dc.level ?? "UNKNOWN"}</span>
                  {" · "}{(w.recycled_wallets ?? []).length} recycled early buyers{sm.reason ? ` · ${sm.reason}` : ""}</div></Stat>
              {pm && <Stat label="Post-migration state">{pm.state}<div className="muted small">{(pm.evidence ?? []).join("; ")}</div></Stat>}
              {intel.trades_to_reach?.checkpoints && <Stat label="Trades to reach SOL on the curve">
                <span className="small mono">{Object.entries(intel.trades_to_reach.checkpoints).map(([k, v]: [string, any]) => `${k} SOL: ${v?.trades ?? "not reached"}`).join(" · ")}</span></Stat>}
              {shadow && <Stat label="Shadow model scores" hint="review only — never used for decisions or sizing">
                <Brain size={14} aria-hidden /> <span className="small mono">{Object.entries(shadow.scores ?? {}).map(([k, v]: [string, any]) => `${k} ${Number(v.value).toFixed(2)}`).join(" · ") || "none yet"}</span></Stat>}
            </div>
            {Array.isArray(intel.snapshots) && intel.snapshots.length > 0 && (
              <div className="table-scroll">
                <table className="data-table">
                  <caption className="table-caption">Launch snapshots (each from trades up to that moment only)</caption>
                  <thead><tr><th>T+</th><th>Market cap</th><th>Buyers / sellers</th><th>Buy / sell SOL</th><th>Curve</th><th>Trades</th></tr></thead>
                  <tbody>{intel.snapshots.map((s: J) => (
                    <tr key={s.offset_seconds}><td>{s.offset_seconds}s</td><td className="mono"><MarketCap sol={s.market_cap_sol} historical compact /></td>
                      <td className="mono">{s.unique_buyers ?? "—"} / {s.unique_sellers ?? "—"}</td>
                      <td className="mono">{show(s.buy_volume_sol, 3)} / {show(s.sell_volume_sol, 3)}</td>
                      <td className="mono">{s.curve_progress !== undefined ? `${(s.curve_progress * 100).toFixed(1)}%` : "unknown"}</td>
                      <td className="mono">{s.trades}</td></tr>))}</tbody>
                </table>
              </div>
            )}
          </>
        )}
      </Section>
      <Section title="Decision history and what followed">
        {q.error ? <ErrorNotice error={q.error} /> : !q.data ? <Loading /> : rows.length === 0 ? <Empty>No recorded decision yet.</Empty> : (
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>Decided</th><th>Stage / decision</th><th>Why</th><th>Outcome</th><th /></tr></thead>
              <tbody>{rows.map((r) => (
                <Fragment key={r.id}>
                  <tr>
                    <td>{formatDate(r.decided_at)}</td><td>{r.stage} · {r.decision}{r.traded ? " · traded" : ""}</td>
                    <td className="muted">{(r.reasons ?? []).slice(0, 2).join("; ") || "—"}</td>
                    <td>{r.analysis?.counterfactual?.classification ?? r.post_exit?.classification ?? r.status}</td>
                    <td><button className="btn btn-sm" onClick={() => setOpen(open === r.id ? null : r.id)} aria-expanded={open === r.id}>
                      {open === r.id ? "Hide" : "Path"}</button></td>
                  </tr>
                  {open === r.id && <tr><td colSpan={5}><PathView row={r} /><RowAnalysis row={r} /></td></tr>}
                </Fragment>))}</tbody>
            </table>
          </div>
        )}
      </Section>
    </>
  );
}
