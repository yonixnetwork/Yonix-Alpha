"use client";

import { ErrorNotice, Loading, Section } from "@/components/ui";
import { formatDate, formatUsdCompact } from "@/lib/format";
import { useProfile } from "@/lib/profile";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;

const CHAIN: Record<string, string> = { solana: "Solana", bsc: "BSC", robinhood: "Robinhood Chain" };
const STATUS_PILL: Record<string, string> = {
  LIVE: "pill pill-ok", PAPER: "pill pill-off", STALE: "pill pill-warn", UNAVAILABLE: "pill pill-danger", NOT_CONFIGURED: "pill pill-off",
};
const COLS: [string, string][] = [["total", "Total"], ["available", "Available"], ["reserved", "Reserved"],
  ["gas_reserve", "Gas reserve"], ["trading_balance", "Trading balance"]];

function Amount({ row, k }: { row: J; k: string }) {
  const v = row[k];
  if (v === null || v === undefined) return <span className="muted">—</span>;
  const usd = row.usd?.[k];
  return (
    <>
      {Number(v).toLocaleString(undefined, { maximumFractionDigits: 6 })} {row.currency}
      {usd !== null && usd !== undefined && <div className="muted small">{formatUsdCompact(usd)}</div>}
    </>
  );
}

/** The YonixAlpha Trading Wallet (master §56-58): one wallet, a Solana account and an EVM account (BSC + Robinhood). */
export default function TradingWallet() {
  const { data, error, loading } = useApi<J>("/api/wallets/overview", undefined, { refreshMs: 30000 });
  const profile = useProfile();
  return (
    <Section title="YonixAlpha Trading Wallet">
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {data && (
        <>
          <p className="muted small">{data.note}</p>
          <div className="stat-grid">
            <div className="stat"><div className="stat-label">Solana account (SOL)</div>
              <div className="stat-value small mono">{data.accounts.solana.address ?? "not synced"}</div></div>
            {profile.evmOn && <div className="stat"><div className="stat-label">EVM account (BSC: BNB, Robinhood Chain: ETH)</div>
              <div className="stat-value small mono">{data.accounts.evm.address ?? "not configured"}</div>
              <div className="form-hint">{data.accounts.evm.status}</div></div>}
          </div>
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>Chain</th><th>Mode</th><th>Status</th>{COLS.map(([k, l]) => <th key={k}>{l}</th>)}<th>Updated</th></tr></thead>
              <tbody>{(data.rows as J[]).filter((r) => profile.chainOn(r.chain)).map((r) => (
                <tr key={r.chain + r.mode}>
                  <td>{CHAIN[r.chain] ?? r.chain}<div className="muted small">{r.account}</div></td>
                  <td>{r.mode}</td>
                  <td><span className={STATUS_PILL[r.status] ?? "pill pill-off"} title={(r.notes ?? []).join("; ")}>
                    {String(r.status).replaceAll("_", " ")}</span></td>
                  {COLS.map(([k]) => <td key={k} className="small"><Amount row={r} k={k} /></td>)}
                  <td className="small">{r.at ? formatDate(r.at) : <span className="muted">—</span>}
                    {(r.notes ?? []).length > 0 && <div className="muted small">{r.notes.join("; ")}</div>}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          <p className="muted small">
            {Object.entries(data.definitions as Record<string, string>).map(([k, v]) => `${k.replaceAll("_", " ")}: ${v}`).join(" · ")}
          </p>
        </>
      )}
    </Section>
  );
}
