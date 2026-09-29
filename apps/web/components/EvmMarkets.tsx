"use client";

import { useState } from "react";
import { Boxes, CircleSlash, ShieldCheck, ShieldQuestion, ShieldX } from "lucide-react";
import { Empty, ErrorNotice, Loading, Money, PageHeader, Section } from "@/components/ui";
import { formatDate } from "@/lib/format";
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

function Decision({ d }: { d: J | null | undefined }) {
  if (!d) return <span className="muted">—</span>;
  if (d.decision === "PAPER_BUY") return <span className="pill pill-ok">PAPER BUY</span>;
  const first = (d.blockers ?? [])[0];
  return (
    <span title={(d.blockers ?? []).map((b: J) => `${b.code}: ${b.message ?? ""}`).join("\n")}>
      <CircleSlash size={12} aria-hidden className="muted" /> {first ? first.code.replaceAll("_", " ") : "NO TRADE"}
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
            <thead><tr><th>Token</th><th>Category</th><th>Opened</th><th>Cost</th><th>Unrealized</th><th>Stop</th><th>Venue</th></tr></thead>
            <tbody>
              {data.positions.map((p: J) => (
                <tr key={p.id}>
                  <td className="mono small">{p.symbol}</td>
                  <td>{p.category ?? "—"}</td>
                  <td>{formatDate(p.entry_at)}</td>
                  <td>{Number(p.entry_cost).toFixed(4)} <span className="unit">{unit}</span></td>
                  <td><Money value={p.unrealized_pnl} currency={unit} digits={6} /></td>
                  <td className="mono small">{price(p.trailing_stop ?? p.stop_loss)}</td>
                  <td className="small">{p.venue?.launchpad} · {p.venue?.route}</td>
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
  const unit = CHAINS.find((c) => c[0] === chain)?.[2] ?? "";
  const { data, error, loading } = useApi<J>("/api/evm/tokens", { chain, ...(category ? { category } : {}) }, { refreshMs: 15000 });
  return (
    <div>
      {header && <PageHeader title="EVM Markets" icon={<Boxes size={20} aria-hidden />}
        subtitle="Tokens discovered on BSC and Robinhood Chain launchpads: category, on-chain safety and the last entry decision. Paper only; a launchpad trades on paper only after its evidence is verified." />}
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
              <thead><tr><th>Token</th><th>Launchpad</th><th>Category</th><th>Stage</th><th>Buys / sells (5m)</th><th>Buyers</th><th>Buy volume</th><th>Safety</th><th>Entry decision</th><th>Last trade</th></tr></thead>
              <tbody>
                {data.tokens.map((t: J) => (
                  <tr key={t.token}>
                    <td className="mono small" title={t.token}>{t.symbol ?? t.token.slice(0, 10)}</td>
                    <td>{t.launchpad}</td>
                    <td>{t.category}</td>
                    <td>{t.stage}</td>
                    <td>{t.stats?.buys ?? 0} / {t.stats?.sells ?? 0}</td>
                    <td>{t.stats?.unique_buyers ?? 0}</td>
                    <td>{t.stats?.buy_volume ? `${Number(t.stats.buy_volume).toFixed(3)} ${unit}` : "—"}</td>
                    <td><Verdict v={t.safety_verdict} /></td>
                    <td className="small"><Decision d={t.entry_decision} /></td>
                    <td>{formatDate(t.last_trade_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  );
}
