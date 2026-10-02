"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { Boxes, ShieldCheck, ShieldQuestion, ShieldX } from "lucide-react";
import { EvmBuyButton } from "@/components/EvmManualTrade";
import ExplorerActions from "@/components/ExplorerActions";
import { CoordinationDetail } from "@/components/LaunchCoordination";
import { SellButton } from "@/components/ManualTrade";
import { PnlDetails } from "@/components/Pnl";
import { Empty, ErrorNotice, Loading, PageHeader, Section, Stat } from "@/components/ui";
import { formatDate, formatUsdCompact } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const CHAIN_NAME: Record<string, string> = { bsc: "BSC", robinhood: "Robinhood Chain" };

function n(v: string | number | null | undefined, digits = 6): string {
  if (v === null || v === undefined || v === "") return "—";
  const x = Number(v);
  return Number.isFinite(x) ? x.toLocaleString(undefined, { maximumSignificantDigits: digits, maximumFractionDigits: 20 }) : "—";
}

function Missing({ why }: { why?: string | null }) {
  return <span className="muted" title={why ?? undefined}>— {why ? <span className="small">({why})</span> : null}</span>;
}

function Verdict({ v }: { v: string | null }) {
  if (!v) return <span className="pill pill-off"><ShieldQuestion size={12} aria-hidden /> not checked</span>;
  const Icon = v === "PASS" ? ShieldCheck : v === "FAIL" ? ShieldX : ShieldQuestion;
  const cls = v === "PASS" ? "pill pill-ok" : v === "FAIL" ? "pill pill-danger" : "pill pill-warn";
  return <span className={cls}><Icon size={12} aria-hidden /> {v}</span>;
}

/** One BSC / Robinhood Chain token (master §54): market (USD market cap),
 * volume, buyers / sellers, safety, launchpad, migration, status, smart
 * money, manipulation, ML, positions, and the explorer actions of its own
 * chain. Manual BUY (paper) runs every entry check; SELL closes a position. */
export default function EvmTokenPage() {
  const { chain, address } = useParams<{ chain: string; address: string }>();
  const { data: t, error, loading } = useApi<J>(`/api/explorer/token/${chain}/${address}`, undefined,
    { refreshMs: 15000, reloadOn: ["position.updated", "trade.opened", "trade.closed"] });
  if (error) return <ErrorNotice error={error} />;
  if (loading && !t) return <Loading />;
  if (!t) return null;
  const m = t.market ?? {};
  const cur = m.currency || (chain === "bsc" ? "BNB" : "ETH");
  const reasons = (m.reasons ?? []).join("; ");
  const openPos = (t.positions ?? []).find((p: J) => p.status === "open");
  const buyBlocked = t.launchpad.observe_only ? `${t.launchpad.name} is observe only: it is not traded`
    : openPos ? "a paper position on this token is open" : null;
  return (
    <div>
      <PageHeader title={`${t.symbol ?? address.slice(0, 10)}${t.name ? ` · ${t.name}` : ""}`} icon={<Boxes size={20} aria-hidden />}
        subtitle={`${CHAIN_NAME[chain] ?? chain} · ${t.launchpad.name}${t.launchpad.observe_only ? " (observe only)" : ""} · ${t.status.category ?? "—"} · ${t.status.stage ?? "—"}`}>
        <EvmBuyButton chain={chain} token={t.address} disabledReason={buyBlocked} />
      </PageHeader>
      <p className="mono small muted">{t.address}</p>
      <ExplorerActions links={t.links} unavailable={t.unavailable} explorerName={t.explorer_name} hide={["token"]} />

      <Section title="Market">
        <div className="stat-grid">
          <Stat label="Market cap (USD)" hint="price x total supply">{m.market_cap_usd ? formatUsdCompact(m.market_cap_usd) : <Missing why={reasons} />}</Stat>
          <Stat label="Price (USD)">{m.price_usd ? `$${n(m.price_usd, 4)}` : <Missing why={reasons} />}</Stat>
          <Stat label={`Price (${cur})`}>{m.price_native ? n(m.price_native, 4) : <Missing why={reasons} />}</Stat>
          <Stat label="Liquidity">{m.liquidity_usd ? formatUsdCompact(m.liquidity_usd) : <Missing why={reasons} />}
            {m.liquidity_native && <div className="muted small">{n(m.liquidity_native, 5)} {cur}</div>}</Stat>
          <Stat label="Total supply">{m.total_supply ? n(m.total_supply, 8) : <Missing why="not read yet" />}</Stat>
          <Stat label={`${cur}/USD`}>{t.native_usd?.price ? `$${n(t.native_usd.price, 6)}` : <Missing why={t.native_usd?.reason} />}</Stat>
        </div>
      </Section>

      <Section title="Activity">
        <div className="stat-grid">
          <Stat label={`Buy volume (${t.volume.window_s ? `${Math.round(t.volume.window_s / 60)}m` : "window"})`}>{t.volume.buy_volume != null ? `${n(t.volume.buy_volume, 5)} ${cur}` : <Missing why="no trades in the window" />}</Stat>
          <Stat label="Sell volume">{t.volume.sell_volume != null ? `${n(t.volume.sell_volume, 5)} ${cur}` : <Missing />}</Stat>
          <Stat label="Buyers (window / 14 d)">{t.buyers.window ?? "—"} / {t.buyers.retained_14d}</Stat>
          <Stat label="Sellers (window / 14 d)">{t.sellers.window ?? "—"} / {t.sellers.retained_14d}</Stat>
          <Stat label="Buys / sells (14 d)">{t.buyers.buys_14d} / {t.sellers.sells_14d}</Stat>
          <Stat label="Holders"><Missing why={t.holders.reason} /></Stat>
          <Stat label="Created">{formatDate(t.created_at)}</Stat>
          <Stat label="Last trade">{formatDate(t.last_trade_at)}</Stat>
        </div>
        <p className="form-hint">{t.volume.note}</p>
      </Section>

      <Section title="Safety">
        <p><Verdict v={t.safety.verdict} /> <span className="muted small">{t.safety.at ? `checked ${formatDate(t.safety.at)}` : ""}</span></p>
        {t.safety.findings.length === 0 ? <Empty>No findings recorded.</Empty> : (
          <table className="data-table">
            <thead><tr><th>Level</th><th>Code</th><th>Detail</th></tr></thead>
            <tbody>{t.safety.findings.map((f: J, i: number) => (
              <tr key={i}><td><span className={f.level === "FAIL" ? "pill pill-danger" : f.level === "PASS" ? "pill pill-ok" : "pill pill-warn"}>{f.level}</span></td>
                <td className="mono small">{f.code}</td><td className="small">{f.message}</td></tr>))}
            </tbody>
          </table>
        )}
      </Section>

      <Section title="Status, launchpad and migration">
        <dl className="term-kv">
          <dt>Launchpad</dt><dd>{t.launchpad.name} {t.launchpad.observe_only && <span className="pill pill-warn">observe only</span>}</dd>
          <dt>Category / stage</dt><dd>{t.status.category ?? "—"} / {t.status.stage ?? "—"}</dd>
          <dt>Migration</dt><dd>{t.migration.migrated_at ? `migrated ${formatDate(t.migration.migrated_at)}` : t.migration.stage === "CURVE" ? "on the bonding curve" : t.migration.stage ?? "—"}</dd>
          <dt>Last automatic decision</dt><dd>{t.status.entry_decision ? `${t.status.entry_decision.decision}${(t.status.entry_decision.blockers ?? []).length ? `: ${t.status.entry_decision.blockers.map((b: J) => b.code).join(", ")}` : ""}` : "—"}</dd>
          <dt>Last manual decision</dt><dd>{t.status.manual_decision ? `${t.status.manual_decision.decision}${(t.status.manual_decision.blockers ?? []).length ? `: ${t.status.manual_decision.blockers.map((b: J) => b.code).join(", ")}` : ""}` : "—"}</dd>
          <dt>Smart money</dt><dd>{t.smart_money.copy_targets_traded.length} copy target(s), {t.smart_money.validated_wallets_traded.length} validated wallet(s) traded it
            <div className="muted small">{t.smart_money.note}. A wallet buying is not a reason to buy.</div></dd>
          <dt>ML</dt><dd><span className="pill pill-off">{t.ml.status}</span> <span className="muted small">{t.ml.reason}</span></dd>
        </dl>
      </Section>

      <Section title="Manipulation (launch-window coordination)">
        <CoordinationDetail chain={chain} token={t.address} />
      </Section>

      <Section title="Positions">
        {t.positions.length === 0 ? <Empty>No paper positions on this token.</Empty> : t.positions.map((p: J) => (
          <div key={p.id} className="card" style={{ marginBottom: 12 }}>
            <div className="btn-row">
              <Link className="link" href={`/dashboard/trades/${p.id}`}>{p.status} · {p.mode} · opened {formatDate(p.entry_at)}</Link>
              {p.source === "manual" && <span className="pill pill-off">manual</span>}
              {p.exit_reason && <span className="muted small">exit: {p.exit_reason}</span>}
              {p.status === "open" && !p.exit_requested && (
                <SellButton positionId={p.id} symbol={t.symbol} mode="PAPER" route={null}
                  description={<>Paper sell of the whole remaining <b>{t.symbol}</b> at the executable sell quote of the position&apos;s current
                    venue, with its fees and token taxes, on the position manager&apos;s next cycle. EVM live execution is locked.</>} />
              )}
              {p.exit_requested && p.status === "open" && <span className="pill pill-warn">exit pending</span>}
            </div>
            <PnlDetails pnl={p.pnl} currency={cur} />
          </div>
        ))}
      </Section>
    </div>
  );
}
