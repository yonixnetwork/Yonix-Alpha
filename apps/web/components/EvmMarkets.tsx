"use client";

import Link from "next/link";
import { useState } from "react";
import { Boxes, CircleSlash, ShieldCheck, ShieldQuestion, ShieldX } from "lucide-react";
import { ObservationPanel, TokenObservations } from "@/components/EvmObservation";
import { CoordinationDetail, CoordinationPanel, CoordinationPill } from "@/components/LaunchCoordination";
import { SellButton } from "@/components/ManualTrade";
import { PnlOutcome } from "@/components/Pnl";
import { Empty, ErrorNotice, Loading, PageHeader, Section } from "@/components/ui";
import { formatDate, formatUsdCompact } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const CHAINS: [string, string, string][] = [["bsc", "BSC", "BNB"], ["robinhood", "Robinhood Chain", "ETH"]];
const CATEGORIES = ["", "FRESH", "MIGRATED", "MOMENTUM", "OTHER"];
const VERDICT_CLASS: Record<string, string> = { PASS: "pill pill-ok", WARN: "pill pill-warn", FAIL: "pill pill-danger", UNKNOWN: "pill pill-off" };

function Verdict({ v }: { v: string | null }) {
  if (!v) return <span className="pill pill-off"><ShieldQuestion size={12} aria-hidden /> not checked</span>;
  const Icon = v === "PASS" ? ShieldCheck : v === "FAIL" ? ShieldX : ShieldQuestion;
  return <span className={VERDICT_CLASS[v] ?? "pill pill-off"}><Icon size={12} aria-hidden /> {v}</span>;
}

const STATE_CLASS: Record<string, string> = {
  EXECUTE: "pill pill-ok", REDUCE_SIZE: "pill pill-ok", WAIT: "pill pill-off", MANUAL_APPROVAL: "pill pill-warn",
  REJECT: "pill pill-danger", NO_TRADE: "pill pill-off",
};

/** Master §77 decision: the state, the §76 layer that decided, the first blocker (all on hover). */
function Decision({ d }: { d: J | null | undefined }) {
  if (!d) return <span className="muted">—</span>;
  if (d.decision === "PAPER_BUY" || d.decision === "EXECUTE") return <span className="pill pill-ok">PAPER BUY</span>;
  if (d.decision === "REDUCE_SIZE") return <span className="pill pill-ok">PAPER BUY (reduced size)</span>;
  const first = (d.blockers ?? [])[0];
  const state = d.decision === "NO_TRADE" || !STATE_CLASS[d.decision] ? "NO TRADE" : d.decision.replaceAll("_", " ");
  return (
    <span title={[d.layer ? `decided by ${d.layer.replaceAll("_", " ")}` : "", ...(d.blockers ?? []).map((b: J) =>
      `${b.layer ? `[${b.layer.replaceAll("_", " ")}] ` : ""}${b.code}: ${b.message ?? ""}`)].filter(Boolean).join("\n")}>
      <span className={STATE_CLASS[d.decision] ?? "pill pill-off"}><CircleSlash size={12} aria-hidden /> {state}</span>
      {first ? <span className="small"> {first.code.replaceAll("_", " ")}</span> : null}
      {(d.blockers ?? []).length > 1 ? <span className="muted small"> +{d.blockers.length - 1}</span> : null}
    </span>
  );
}

/** Per-token prices are tiny (e.g. 0.00000085 BNB): significant digits, never exponent notation. */
function price(v: string | null | undefined): string {
  const n = Number(v);
  if (v === null || v === undefined || !Number.isFinite(n)) return "—";
  return n.toLocaleString(undefined, { maximumSignificantDigits: 4, maximumFractionDigits: 20 });
}

function Positions({ chain, unit }: { chain: string; unit: string }) {
  const { data, error, loading } = useApi<J>("/api/evm/positions", { chain }, { refreshMs: 10000, reloadOn: ["position.updated", "trade.opened", "trade.closed"] });
  return (
    <Section title="Paper positions">
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {data && (
        <p className="muted small">
          Account: {data.accounts.map((a: J) => `${a.name} cash ${Number(a.cash).toFixed(4)} ${a.currency}`).join(" · ") || "not created yet"} ·
          positions are marked at the executable sell quote of the remaining tokens (fees and taxes included)
        </p>
      )}
      {data && data.positions.length === 0 && <Empty>No open paper positions on this chain.</Empty>}
      {data && data.positions.length > 0 && (
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Token</th><th>Category</th><th>Opened</th><th>Cost</th><th>Value</th><th>PnL</th><th>Peak / drawdown</th><th>Stop</th><th>Venue</th><th /></tr></thead>
            <tbody>
              {data.positions.map((p: J) => (
                <tr key={p.id}>
                  <td className="mono small">
                    <Link className="link" href={`/dashboard/explorer/${p.chain}/${p.token}`}>{p.symbol}</Link>
                    {p.entry_source === "manual" && <span className="pill pill-off"> manual</span>}
                    {p.exit_requested && <span className="pill pill-warn"> exit pending</span>}
                  </td>
                  <td>{p.category ?? "—"}</td>
                  <td>{formatDate(p.entry_at)}</td>
                  <td>{Number(p.entry_cost).toFixed(4)} <span className="unit">{unit}</span></td>
                  <td>{p.pnl?.value ? <>{price(p.pnl.value)} <span className="unit">{unit}</span></> : "—"}</td>
                  <td><PnlOutcome pnl={p.pnl} currency={unit} /></td>
                  <td className="small">{p.pnl?.peak_pct != null ? `+${p.pnl.peak_pct}%` : "—"} / <span className={Number(p.pnl?.drawdown_pct) < 0 ? "neg" : ""}>{p.pnl?.drawdown_pct != null ? `${p.pnl.drawdown_pct}%` : "—"}</span></td>
                  <td className="mono small">{price(p.trailing_stop ?? p.stop_loss)}</td>
                  <td className="small">{p.venue?.launchpad} · {p.venue?.route}</td>
                  <td>{p.status === "open" && !p.exit_requested && (
                    <SellButton positionId={p.id} symbol={p.symbol} mode="PAPER" route={p.venue?.route ?? null}
                      description={<>Paper sell of the whole remaining <b>{p.symbol}</b> at the executable sell quote of the position&apos;s
                        current venue (the launchpad curve, or the DEX after migration), with its fees and token taxes, on the next
                        cycle of the {unit === "BNB" ? "BSC" : "Robinhood Chain"} position manager. EVM live execution is locked: nothing is sent on chain.</>} />
                  )}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

/** EVM markets for one chain (fixedChain) or with chain tabs. */
export default function EvmMarkets({ fixedChain, header = true }: { fixedChain?: string; header?: boolean }) {
  const [chosen, setChain] = useState("bsc");
  const chain = fixedChain ?? chosen;
  const [category, setCategory] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const unit = CHAINS.find((c) => c[0] === chain)?.[2] ?? "";
  const { data, error, loading } = useApi<J>("/api/evm/tokens", { chain, ...(category ? { category } : {}) }, { refreshMs: 15000 });
  return (
    <div>
      {header && <PageHeader title="EVM Markets" icon={<Boxes size={20} aria-hidden />}
        subtitle="Tokens discovered on BSC and Robinhood Chain launchpads: category, on-chain safety, launch-window coordination and the last entry decision. Paper only; a launchpad trades on paper only after its evidence is verified." />}
      <div role="tablist" style={{ display: "flex", gap: 8, margin: "12px 0", flexWrap: "wrap" }}>
        {!fixedChain && CHAINS.map(([v, label]) => (
          <button key={v} role="tab" aria-selected={chain === v} className={chain === v ? "btn btn-sm" : "btn btn-ghost btn-sm"} onClick={() => setChain(v)}>{label}</button>
        ))}
        <span style={{ width: 16 }} />
        {CATEGORIES.map((c) => (
          <button key={c || "all"} aria-pressed={category === c} className={category === c ? "btn btn-sm" : "btn btn-ghost btn-sm"} onClick={() => setCategory(c)}>
            {c || "All"}{c && data?.categories?.[c] !== undefined ? ` (${data.categories[c]})` : ""}
          </button>
        ))}
      </div>
      <Positions chain={chain} unit={unit} />
      <Section title="Tokens active in the last hour">
        <ErrorNotice error={error} />
        {loading && !data && <Loading />}
        {data && data.tokens.length === 0 && <Empty>No tokens discovered yet on this chain (is the data-evm service running?).</Empty>}
        {data && data.tokens.length > 0 && (
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>Token</th><th>Launchpad</th><th>Category</th><th>Stage</th><th>Buys / sells (5m)</th><th>Buyers</th><th>Buy volume</th><th>Market cap</th><th>Safety</th><th>Coordination</th><th>Entry decision</th><th>Last trade</th></tr></thead>
              <tbody>
                {data.tokens.map((t: J) => (
                  <tr key={t.token} onClick={() => setSelected(selected === t.token ? null : t.token)} style={{ cursor: "pointer" }}
                    aria-selected={selected === t.token} title="Show the launch-coordination assessment">
                    <td className="mono small" title={t.token}>
                      <Link className="link" href={`/dashboard/explorer/${t.chain}/${t.token}`} onClick={(e) => e.stopPropagation()}>{t.symbol ?? t.token.slice(0, 10)}</Link>
                    </td>
                    <td>{t.launchpad}</td>
                    <td>{t.category}</td>
                    <td>{t.stage}</td>
                    <td>{t.stats?.buys ?? 0} / {t.stats?.sells ?? 0}</td>
                    <td>{t.stats?.unique_buyers ?? 0}</td>
                    <td>{t.stats?.buy_volume ? `${Number(t.stats.buy_volume).toFixed(3)} ${unit}` : "—"}</td>
                    <td title={(t.market?.reasons ?? []).join("; ") || "price x total supply, in USD"}>{t.market?.market_cap_usd ? formatUsdCompact(t.market.market_cap_usd) : <span className="muted">—</span>}</td>
                    <td><Verdict v={t.safety_verdict} /></td>
                    <td><CoordinationPill action={t.coordination_action} status={t.coordination_status} /></td>
                    <td className="small"><Decision d={t.entry_decision} /></td>
                    <td>{formatDate(t.last_trade_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
      {selected && (
        <>
          <Section title={`Observation: ${data?.tokens.find((t: J) => t.token === selected)?.symbol ?? selected.slice(0, 10)}`}>
            <TokenObservations chain={chain} token={selected} />
          </Section>
          <Section title={`Launch coordination: ${data?.tokens.find((t: J) => t.token === selected)?.symbol ?? selected.slice(0, 10)}`}>
            <CoordinationDetail chain={chain} token={selected} />
          </Section>
        </>
      )}
      <ObservationPanel chain={chain} onSelect={setSelected} />
      <CoordinationPanel chain={chain} />
    </div>
  );
}
