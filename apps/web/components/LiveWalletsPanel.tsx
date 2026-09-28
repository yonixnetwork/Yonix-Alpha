"use client";

import { useState } from "react";
import { ErrorNotice, Loading, Money, Section, Stat } from "@/components/ui";
import { ApiError, apiPost } from "@/lib/api";
import { formatDate, formatDecimal } from "@/lib/format";
import { useApi } from "@/lib/useApi";

interface RentReclaim {
  id: string; status: string; reason: string; scope: string; signature: string | null; error: string | null;
  created_at: string; closed: number; refund_sol: string | null;
}

interface Holding {
  mint: string; quantity: string | null; price_sol: string | null; value_sol: string | null; value_usd: string | null;
  price_source: string | null; price_at: string | null; status: string; reason?: string | null;
}
interface Wallets {
  live: {
    label: "LIVE"; synced: boolean; address?: string | null; sol?: string; balance_at?: string; balance_age_seconds?: number;
    balance_status?: string; reserved_sol?: string; available_sol?: string; holdings?: Holding[];
    holdings_value_sol?: string | null; holdings_unvalued?: number | null; valuation_note?: string | null;
    sol_usd?: string | null; sol_usd_source?: string | null; total_estimated_sol?: string | null; total_is_complete?: boolean;
    empty_token_accounts?: { count: number; rent_sol: string } | null; rent_reclaims?: RentReclaim[];
  };
  paper: {
    label: "PAPER"; currency: string; starting_balance: string; available_balance: string; open_positions: number;
    open_position_value: string; unrealized_pnl: string; realized_pnl: string; equity: string; since: string;
  };
}

/** Real wallet (from the chain, public address only) and the paper book,
 * side by side and never mixed. */
export default function LiveWalletsPanel() {
  const { data, error, reload } = useApi<Wallets>("/api/live/wallets", undefined, {
    refreshMs: 10000, reloadOn: ["balance.updated", "trade.created", "trade.closed"],
  });
  const [reclaimMsg, setReclaimMsg] = useState<string | null>(null);
  async function reclaim(count: number, sol: string) {
    if (!window.confirm(`Close ${count} empty token account(s) and return ${sol} SOL of rent to the wallet?\n\n`
      + "Only accounts holding zero tokens, with no open or pending position, are closed. The SOL can only go back to this wallet.")) return;
    setReclaimMsg(null);
    try {
      await apiPost("/api/live/reclaim-rent", { confirm: true });
      setReclaimMsg("Queued: the order worker closes the empty accounts now.");
      reload();
    } catch (err) {
      setReclaimMsg(err instanceof ApiError ? err.message : "Request failed.");
    }
  }
  if (!data) {
    return (
      <Section title="Wallets — LIVE and PAPER (never mixed)">
        {error ? <ErrorNotice error={error} /> : <Loading what="Loading wallets…" />}
      </Section>
    );
  }
  const { live, paper } = data;
  return (
    <Section title="Wallets — LIVE and PAPER (never mixed)">
      <div className="status-label"><span className="pill pill-danger">LIVE</span> real wallet, read from the chain</div>
      {!live.synced ? (
        <div className="muted">No wallet sync yet: the live order worker has not reported a balance.</div>
      ) : (
        <>
          <div className="stat-grid">
            <Stat label="Address">{live.address}</Stat>
            <Stat label="SOL balance" hint={`${live.balance_status} · ${formatDate(live.balance_at ?? null)}`}>
              {formatDecimal(live.sol ?? null, 6)} {live.balance_status !== "LIVE" && <span className="pill pill-warn">STALE</span>}
            </Stat>
            <Stat label="Available SOL">{formatDecimal(live.available_sol ?? null, 6)}</Stat>
            <Stat label="Reserved SOL" hint="min_sol_reserve + pending buys">{formatDecimal(live.reserved_sol ?? null, 6)}</Stat>
            <Stat label="Token holdings (SOL)" hint={live.holdings_unvalued ? `${live.holdings_unvalued} not valued` : undefined}>
              {formatDecimal(live.holdings_value_sol ?? null, 6)}
            </Stat>
            <Stat label="Total estimated (SOL)" hint={live.total_is_complete ? "all holdings valued" : "some holdings NOT valued"}>
              {formatDecimal(live.total_estimated_sol ?? null, 6)}
            </Stat>
          </div>
          {(live.holdings ?? []).length > 0 && (
            <table className="data-table">
              <thead><tr><th>Token</th><th>Quantity</th><th>Price (SOL)</th><th>Value (SOL)</th><th>Value (USD)</th><th>Source / time</th></tr></thead>
              <tbody>{(live.holdings ?? []).map((h) => (
                <tr key={h.mint}>
                  <td><code>{h.mint.slice(0, 4)}…{h.mint.slice(-4)}</code></td>
                  <td>{formatDecimal(h.quantity, 2)}</td>
                  <td>{h.price_sol ? formatDecimal(h.price_sol, 12) : <span className="pill pill-warn">{h.status}</span>}</td>
                  <td>{h.value_sol ? <>{formatDecimal(h.value_sol, 6)}{h.status === "STALE" && <span className="pill pill-warn"> STALE</span>}</> : "—"}</td>
                  <td>{h.value_usd ?? (h.value_sol ? <span className="muted">SOL/USD unavailable</span> : "—")}</td>
                  <td className="muted">{h.price_source ? `${h.price_source} · ${formatDate(h.price_at)}` : h.reason}</td>
                </tr>))}</tbody>
            </table>
          )}
          <div className="muted">{live.valuation_note}{live.sol_usd ? ` · SOL/USD ${live.sol_usd} (${live.sol_usd_source})` : ""}</div>
          {live.empty_token_accounts && (
            <div className="rent-row">
              <span>
                Empty token accounts: <b>{live.empty_token_accounts.count}</b>, holding{" "}
                <b className="mono">{formatDecimal(live.empty_token_accounts.rent_sol, 6)} SOL</b> of rent
                <span className="muted"> (returned to the wallet when the account is closed)</span>
              </span>
              {live.empty_token_accounts.count > 0 && (
                <button className="btn btn-sm" type="button"
                  onClick={() => reclaim(live.empty_token_accounts!.count, live.empty_token_accounts!.rent_sol)}>
                  Return rent to wallet
                </button>
              )}
            </div>
          )}
          {reclaimMsg && <div className="muted">{reclaimMsg}</div>}
          {(live.rent_reclaims ?? []).length > 0 && (
            <table className="data-table compact">
              <thead><tr><th>Rent reclaim</th><th>Status</th><th>Accounts closed</th><th>Returned (SOL)</th><th>Transaction</th></tr></thead>
              <tbody>{(live.rent_reclaims ?? []).map((r) => (
                <tr key={r.id}>
                  <td className="muted">{formatDate(r.created_at)} · {r.reason === "rent_reclaim_after_exit" ? "after exit" : "manual"}</td>
                  <td><span className={r.status === "CONFIRMED" ? "pill pill-ok" : r.status === "SKIPPED" ? "pill pill-off"
                    : ["PENDING", "SIGNED", "SUBMITTED"].includes(r.status) ? "pill pill-warn" : "pill pill-danger"}>{r.status}</span>
                    {r.error && <div className="muted">{r.error}</div>}</td>
                  <td>{r.status === "CONFIRMED" ? r.closed : "—"}</td>
                  <td className="mono">{r.refund_sol ?? "—"}</td>
                  <td>{r.signature ? <a className="mono" href={`https://solscan.io/tx/${r.signature}`} target="_blank"
                    rel="noopener noreferrer">{r.signature.slice(0, 10)}…</a> : "—"}</td>
                </tr>))}</tbody>
            </table>
          )}
        </>
      )}
      <div className="status-label"><span className="pill pill-off">PAPER</span> simulated book ({paper.currency})</div>
      <div className="stat-grid">
        <Stat label="Starting balance">{formatDecimal(paper.starting_balance, 4)}</Stat>
        <Stat label="Available">{formatDecimal(paper.available_balance, 6)}</Stat>
        <Stat label="Open positions value" hint={`${paper.open_positions} open`}>{formatDecimal(paper.open_position_value, 6)}</Stat>
        <Stat label="Unrealized PnL"><Money value={paper.unrealized_pnl} currency={paper.currency} digits={6} /></Stat>
        <Stat label="Realized PnL" hint={`since ${formatDate(paper.since)}`}><Money value={paper.realized_pnl} currency={paper.currency} digits={6} /></Stat>
        <Stat label="Equity">{formatDecimal(paper.equity, 6)}</Stat>
      </div>
    </Section>
  );
}
