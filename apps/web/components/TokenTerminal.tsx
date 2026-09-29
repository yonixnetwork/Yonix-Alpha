"use client";

import { Copy, ExternalLink } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import LineChart from "@/components/LineChart";
import MarketCap from "@/components/MarketCap";
import { BuyButton, SellButton } from "@/components/ManualTrade";
import { ErrorNotice, Loading, fmtDuration } from "@/components/ui";
import { formatDate, formatDecimal, gateDecisionPillClass } from "@/lib/format";
import { useApi } from "@/lib/useApi";

interface Win { trades: number; tx_per_minute: number; buyers: number; sellers: number; buy_sell_ratio: string | null;
  buy_volume_sol: string; sell_volume_sol: string; volume_sol: string; price_change_pct: string | null;
  price_acceleration_pct: string | null; previous_trades: number }
interface Activity { kind: string; at: string; wallet?: string; sol?: string; tokens?: string; price?: string | null;
  large?: boolean; creator?: boolean; detail?: string }
interface Market {
  mint: string; symbol: string | null; name: string | null; creator: string | null; stream_trades: number;
  header: { price_sol: string | null; price_source: string | null; price_at: string | null; market_cap_sol: string | null;
    market_cap_basis: string; liquidity: { sol: string; basis: string } | null; migration_state: string; age_seconds: number | null;
    risk_status: string | null; decision: string | null };
  windows: Record<string, Win>;
  series: { t: string; price: string; liquidity_sol: string }[];
  volume: { t: string; buy_sol: string; sell_sol: string }[];
  activity: Activity[];
  decision: null | { decision: string; status_label: string; overall_risk: string; reasons: string[]; evaluated_at: string;
    qualified: boolean | null; assessment_id?: string; signal: { code: string; message: string; action: string }[];
    ml: { status?: string; score?: number; model?: string; version?: number } | null;
    entry_quality: { strong?: boolean; indicators?: string[]; evidence?: string[] } | null;
    volatility: { confidence: string | null; source: string | null } };
  links: { dexscreener: string; explorer: string; pump_fun: string; telegram: string | null; note: string };
  position: null | { id: string; status: string; mode: string; route: string | null; entry_price: string; last_price: string | null;
    remaining_quantity: string | null; entry_cost_sol: string | null };
}

const short = (w?: string) => (w ? `${w.slice(0, 4)}…${w.slice(-4)}` : "—");
const signed = (v: string | null | undefined) => (v === null || v === undefined ? "—" : `${Number(v) > 0 ? "+" : ""}${v}%`);
const tone = (v: string | null | undefined) => (v === null || v === undefined ? "muted" : Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : "");

function ExtLink({ href, label }: { href: string; label: string }) {
  return (
    <a className="ext-link" href={href} target="_blank" rel="noopener noreferrer">
      {label} <ExternalLink size={12} aria-hidden />
    </a>
  );
}

function WindowStats({ name, w }: { name: string; w: Win }) {
  return (
    <div className="term-card">
      <div className="term-card-title">Last {name}</div>
      <dl className="term-kv">
        <dt>Buyers / sellers</dt><dd><span className="pos">{w.buyers}</span> / <span className="neg">{w.sellers}</span></dd>
        <dt>Buy / sell ratio</dt><dd>{w.buy_sell_ratio ?? "—"}</dd>
        <dt>Volume</dt><dd className="mono">{formatDecimal(w.volume_sol, 3)} SOL</dd>
        <dt>Buy / sell volume</dt><dd className="mono"><span className="pos">{formatDecimal(w.buy_volume_sol, 3)}</span> / <span className="neg">{formatDecimal(w.sell_volume_sol, 3)}</span></dd>
        <dt>Transactions</dt><dd>{w.trades} <span className="muted">({w.tx_per_minute}/min, prev {w.previous_trades})</span></dd>
        <dt>Price change</dt><dd className={tone(w.price_change_pct)}>{signed(w.price_change_pct)}</dd>
        <dt>Price acceleration</dt><dd className={tone(w.price_acceleration_pct)}>{signed(w.price_acceleration_pct)}</dd>
      </dl>
    </div>
  );
}

/** Lightweight token terminal: header, chart, flow windows, trade controls,
 * decision, live activity and external links — all from recorded data. */
export default function TokenTerminal({ mint }: { mint: string }) {
  const { data, error } = useApi<Market>(`/api/tokens/${mint}/market`, undefined, {
    refreshMs: 4000, reloadOn: ["risk.updated", "trade.created", "trade.closed", "trade.updated"],
  });
  const [copied, setCopied] = useState(false);
  if (error) return <ErrorNotice error={error} />;
  if (!data) return <Loading />;
  const h = data.header;
  const d = data.decision;
  const priceSeries = data.series.map((p) => ({ x: p.t, y: Number(p.price) }));
  const liqSeries = data.series.map((p) => ({ x: p.t, y: Number(p.liquidity_sol) }));
  const volSeries = data.volume.map((v) => ({ x: v.t, y: Number(v.buy_sol) - Number(v.sell_sol) }));
  return (
    <div className="terminal">
      <div className="term-header">
        <div className="term-title">
          <h1>{data.symbol ?? "Token"} <span className="muted">{data.name ?? ""}</span></h1>
          <button className="mono copy" type="button" title="Copy mint address"
            onClick={() => { void navigator.clipboard?.writeText(mint); setCopied(true); setTimeout(() => setCopied(false), 1500); }}>
            {mint} <Copy size={12} aria-hidden /> {copied && <span className="muted">copied</span>}
          </button>
          <div className="term-links">
            <ExtLink href={data.links.dexscreener} label="DexScreener" />
            <ExtLink href={data.links.explorer} label="Explorer" />
            <ExtLink href={data.links.pump_fun} label="Pump.fun" />
            {data.links.telegram && <ExtLink href={data.links.telegram} label="Telegram" />}
          </div>
        </div>
        <div className="term-metrics">
          <div><span className="term-label">Price</span><span className="term-value mono">{h.price_sol ? formatDecimal(h.price_sol, 12) : "—"}</span>
            <span className="term-sub">{h.price_source ?? "no price"}{h.price_at ? ` · ${formatDate(h.price_at)}` : ""}</span></div>
          <div><span className="term-label">Market cap</span><span className="term-value mono"><MarketCap sol={h.market_cap_sol} /></span>
            <span className="term-sub" title={h.market_cap_basis}>price × supply (= FDV on Pump.fun)</span></div>
          <div><span className="term-label">Liquidity</span><span className="term-value mono"><MarketCap sol={h.liquidity?.sol} /></span>
            <span className="term-sub">{h.liquidity?.basis ?? "not observed"}</span></div>
          <div><span className="term-label">State</span><span className="term-value">{h.migration_state}</span>
            <span className="term-sub">age {fmtDuration(h.age_seconds)}</span></div>
          <div><span className="term-label">Risk</span><span className={`term-value level-${h.risk_status ?? ""}`}>{h.risk_status ?? "not assessed"}</span>
            <span className="term-sub">{h.decision ?? ""}</span></div>
        </div>
      </div>

      <div className="term-grid">
        <div className="term-main">
          <div className="term-card">
            <LineChart points={priceSeries} label="Price (SOL per token, every stream trade)" height={200} />
          </div>
          <div className="term-row">
            <div className="term-card"><LineChart points={liqSeries} label="Curve liquidity (SOL)" height={110} /></div>
            <div className="term-card"><LineChart points={volSeries} label="Net buy volume per 10 s (SOL)" height={110} /></div>
          </div>
          <div className="term-row">
            {Object.entries(data.windows).map(([k, w]) => <WindowStats key={k} name={k} w={w} />)}
          </div>
        </div>

        <aside className="term-side">
          <div className="term-card">
            <div className="term-card-title">Trade</div>
            <div className="term-actions">
              <BuyButton mint={mint} source="token terminal" />
              {data.position && data.position.status === "open" && (
                <SellButton positionId={data.position.id} symbol={data.symbol} mode={data.position.mode} route={data.position.route} />
              )}
            </div>
            {data.position ? (
              <dl className="term-kv">
                <dt>Position</dt><dd><Link className="link" href={`/dashboard/trades/${data.position.id}`}>{data.position.mode} · {data.position.status}</Link></dd>
                <dt>Entry</dt><dd className="mono">{formatDecimal(data.position.entry_price, 12)}</dd>
                <dt>Last</dt><dd className="mono">{formatDecimal(data.position.last_price, 12)}</dd>
                <dt>Remaining</dt><dd className="mono">{formatDecimal(data.position.remaining_quantity, 2)}</dd>
                <dt>Route</dt><dd>{data.position.route ?? "—"}</dd>
              </dl>
            ) : <p className="muted">BUY shows the route, estimated output, slippage, risk status and balance, then runs the full safety gate.</p>}
          </div>

          <div className="term-card">
            <div className="term-card-title">Decision</div>
            {!d ? <p className="muted">Not assessed yet.</p> : (
              <>
                <div><span className={gateDecisionPillClass(d.decision)}>{d.decision}</span> <span className="muted">{d.status_label}</span></div>
                <dl className="term-kv">
                  <dt>Risk</dt><dd className={`level-${d.overall_risk}`}>{d.overall_risk}</dd>
                  <dt>Signal</dt><dd>{d.qualified ? "qualified" : "not qualified"}</dd>
                  <dt>ML</dt><dd>{d.ml?.score !== undefined ? `${Number(d.ml.score).toFixed(3)} (${d.ml.model} v${d.ml.version})` : d.ml?.status ?? "—"}</dd>
                  <dt>Volatility</dt><dd>{d.volatility?.confidence ?? "—"}</dd>
                  <dt>Assessed</dt><dd>{formatDate(d.evaluated_at)}</dd>
                </dl>
                {(d.entry_quality?.indicators ?? []).length > 0 && (
                  <div className={d.entry_quality?.strong ? "neg" : "muted"}>Deterioration: {(d.entry_quality?.indicators ?? []).join(", ")}</div>
                )}
                <ul className="reason-list">{d.reasons.slice(0, 5).map((r, i) => <li key={i}>{r}</li>)}</ul>
                {d.assessment_id && <Link className="link" href={`/dashboard/decisions/${d.assessment_id}`}>Full decision</Link>}
              </>
            )}
          </div>
        </aside>
      </div>

      <div className="term-card">
        <div className="term-card-title">Live activity <span className="muted">({data.stream_trades} stream trades held · refreshes every 4 s)</span></div>
        {data.activity.length === 0 ? <p className="muted">No trade events held for this token (PumpSwap trades after migration are not in the stream).</p> : (
          <div className="table-scroll activity">
            <table className="data-table compact">
              <thead><tr><th>Time</th><th>Event</th><th>Wallet</th><th>SOL</th><th>Tokens</th><th>Price</th></tr></thead>
              <tbody>{data.activity.map((e, i) => (
                <tr key={i} className={e.large ? "row-large" : undefined}>
                  <td className="muted">{formatDate(e.at)}</td>
                  <td><span className={e.kind === "BUY" ? "pill pill-ok" : e.kind === "SELL" ? "pill pill-danger" : "pill pill-off"}>{e.kind}</span>
                    {e.large && <span className="pill pill-warn">LARGE</span>}{e.creator && <span className="pill pill-warn">CREATOR</span>}</td>
                  <td className="mono">{e.detail ?? short(e.wallet)}</td>
                  <td className="mono">{e.sol ? formatDecimal(e.sol, 4) : ""}</td>
                  <td className="mono">{e.tokens ? formatDecimal(e.tokens, 0) : ""}</td>
                  <td className="mono">{e.price ? formatDecimal(e.price, 12) : ""}</td>
                </tr>))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <p className="muted small">{data.links.note}</p>
    </div>
  );
}
