"use client";

import { useState } from "react";
import { useParams } from "next/navigation";
import { CandlestickChart, KeyRound, Search, TrendingUp, Waves } from "lucide-react";
import DecisionsTable from "@/components/DecisionsTable";
import PerformancePanel from "@/components/PerformancePanel";
import PositionsTable from "@/components/PositionsTable";
import StrategyPanel from "@/components/StrategyPanel";
import { Empty, ErrorNotice, Loading, PageHeader, Section, Stat, StatePill } from "@/components/ui";
import { apiGet, ApiError } from "@/lib/api";
import type { StrategyOut, VenueOut } from "@/lib/cc";
import { formatDate, formatDecimal } from "@/lib/format";
import { useApi } from "@/lib/useApi";

const ENGINE: Record<string, string> = { binance: "binance_futures", bybit: "bybit_futures", hyperliquid: "hyperliquid_perps" };
const ICON: Record<string, React.ReactNode> = {
  binance: <CandlestickChart size={20} aria-hidden />,
  bybit: <TrendingUp size={20} aria-hidden />,
  hyperliquid: <Waves size={20} aria-hidden />,
};
const DEFAULT_SYMBOL: Record<string, string> = { binance: "BTCUSDT", bybit: "BTCUSDT", hyperliquid: "BTC" };

function MarketLookup({ venue }: { venue: string }) {
  const [symbol, setSymbol] = useState(DEFAULT_SYMBOL[venue]);
  const [data, setData] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function look(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setData(await apiGet(`/api/venues/${venue}/market`, { symbol: symbol.trim().toUpperCase() }));
    } catch (err) {
      setData(null);
      setError(err instanceof ApiError ? err.message : "Lookup failed.");
    } finally {
      setBusy(false);
    }
  }
  const t = data?.ticker ?? {};
  const b = data?.book ?? {};
  return (
    <div>
      <form className="inline-form" onSubmit={look}>
        <label htmlFor="sym" className="sr-only">
          Symbol
        </label>
        <input id="sym" value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder={DEFAULT_SYMBOL[venue]} />
        <button className="btn btn-sm" disabled={busy}>
          <Search size={14} aria-hidden /> {busy ? "Fetching…" : "Live quote"}
        </button>
      </form>
      <ErrorNotice error={error} />
      {data && (
        <div className="stat-grid">
          <Stat label="Last">{formatDecimal(t.last_price ?? null, 6)}</Stat>
          <Stat label="Mark">{formatDecimal(t.mark_price ?? null, 6)}</Stat>
          <Stat label="Funding">{t.funding_rate ? `${(Number(t.funding_rate) * 100).toFixed(4)}%` : "—"}</Stat>
          <Stat label="Open interest">{formatDecimal(t.open_interest ?? null, 2)}</Stat>
          <Stat label="Book mid">{formatDecimal(b.mid ?? null, 6)}</Stat>
          <Stat label="Spread">{b.spread_bps ? `${Number(b.spread_bps).toFixed(2)} bps` : "—"}</Stat>
          <Stat label="Depth within band" hint="quote volume within the gate's price band">
            {formatDecimal(b.liquidity_quote_within_band ?? null, 0)}
          </Stat>
          <Stat label="Observed">{formatDate(t.observed_at ?? null)}</Stat>
        </div>
      )}
    </div>
  );
}

function BinanceAccount() {
  const { data, error } = useApi<Record<string, any>>("/api/venues/binance/account", undefined, { refreshMs: 30000 });
  if (error) return <ErrorNotice error={error} />;
  if (!data) return <Loading />;
  return (
    <div>
      <div className="muted">
        From engine-binance-futures&apos; synced tables (the API never calls signed Binance endpoints itself).
      </div>
      {data.positions.length === 0 ? (
        <Empty>No exchange positions recorded.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Amount</th>
                <th>Entry</th>
                <th>Mark</th>
                <th>Unrealized</th>
                <th>Leverage</th>
                <th>Liquidation</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {data.positions.map((p: any) => (
                <tr key={p.symbol}>
                  <td>{p.symbol}</td>
                  <td>{formatDecimal(p.amount, 6)}</td>
                  <td>{formatDecimal(p.entry, 6)}</td>
                  <td>{formatDecimal(p.mark, 6)}</td>
                  <td>{formatDecimal(p.unrealized_pnl, 4)}</td>
                  <td>{p.leverage ?? "—"}</td>
                  <td>{formatDecimal(p.liquidation, 4)}</td>
                  <td className="muted">{formatDate(p.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="muted" style={{ marginTop: 8 }}>
        {data.open_orders.length} open order(s) · {data.recent_fills.length} recent fill(s) ·{" "}
        {data.income_7d.map((i: any) => `${i.type} ${formatDecimal(i.total, 4)} ${i.asset}`).join(", ") || "no income in 7 days"}
      </div>
    </div>
  );
}

function VerifyAccount({ venue }: { venue: string }) {
  const [data, setData] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function verify() {
    setBusy(true);
    setError(null);
    try {
      setData(await apiGet(`/api/venues/${venue}/account`));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Account read failed.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div>
      <button className="btn btn-sm" onClick={verify} disabled={busy}>
        <KeyRound size={14} aria-hidden /> {busy ? "Reading…" : "Read account (read-only)"}
      </button>
      <div className="form-hint">Balances, positions and open orders only; nothing can be placed or cancelled from here.</div>
      <ErrorNotice error={error} />
      {data && <pre className="json">{JSON.stringify(data, null, 2)}</pre>}
    </div>
  );
}

export default function VenuePage() {
  const { venue } = useParams<{ venue: string }>();
  const engine = ENGINE[venue];
  const venues = useApi<VenueOut[]>("/api/venues", undefined, { refreshMs: 30000, reloadOn: ["system.health.updated"] });
  const strat = useApi<StrategyOut>(engine ? `/api/strategies/${engine}` : null, undefined, { reloadOn: ["strategy.updated"] });
  if (!engine) return <ErrorNotice error="Unknown venue." />;
  const v = venues.data?.find((x) => x.venue === venue);
  return (
    <div>
      <PageHeader title={strat.data?.label ?? venue} icon={ICON[venue]} subtitle={`Venue mode key: ${engine}`} />
      <ErrorNotice error={venues.error || strat.error} />
      {v && (
        <div className="card">
          <div className="stat-grid">
            <Stat label="Market data">{v.market_data ? <StatePill state={v.market_data.state} /> : "—"}</Stat>
            <Stat label="Account" hint={v.account.verified_at ? `verified ${formatDate(v.account.verified_at)}` : undefined}>
              <span className={v.account.status.startsWith("VERIFIED") ? "pill pill-ok" : v.account.status === "NOT CONNECTED" ? "pill pill-off" : "pill pill-warn"}>
                {v.account.status}
              </span>
            </Stat>
            <Stat label="Credentials">{v.credentials_configured ? "set in .env" : "not set"}</Stat>
            <Stat label="Paper book">
              {formatDecimal(v.paper_account.cash, 4)} {v.paper_account.currency}
            </Stat>
          </div>
          <div className="muted">{v.market_data?.detail}</div>
          <div className="notice notice-warn">{v.live_orders}</div>
        </div>
      )}
      {strat.data && <StrategyPanel s={strat.data} onChange={strat.setData} />}
      <Section title="Live market data">
        <MarketLookup venue={venue} />
      </Section>
      <Section title="Exchange account">{venue === "binance" ? <BinanceAccount /> : <VerifyAccount venue={venue} />}</Section>
      <Section title="Paper positions">
        <PositionsTable engine={engine} />
      </Section>
      <Section title="Safety-gate decisions">
        <DecisionsTable engine={engine} />
      </Section>
      <Section title="Paper performance">
        {strat.data && <PerformancePanel account={strat.data.account ?? undefined} engine={engine} />}
      </Section>
    </div>
  );
}
