"use client";

import { useState } from "react";
import { Empty, ErrorNotice, Loading, Section, TokenLink } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

interface Pos {
  id: string; mode: string; engine: string; strategy: string | null; symbol: string | null; mint: string | null;
  status: string; signal_at: string | null; entry_at: string; entry_price: number | null; decision_price: number | null;
  exit_at: string | null; exit_price: number | null; size_sol: number | null; gross_pnl_sol: number | null;
  fees_sol: number | null; net_pnl_sol: number | null; net_pnl_pct: number | null;
  entry_slippage_estimate_pct: number | null; hold_s: number | null; exit_reason: string | null;
  unrealized_marked_sol: number | null;
}

const v = (x: unknown) => (x === null || x === undefined ? "—" : String(x));
const sign = (x: number | null) => (x === null ? "" : x > 0 ? "pos" : x < 0 ? "neg" : "");

/** Latest Solana positions one by one (GET /api/analytics/solana-positions):
 * booked PnL only for closed positions; open ones show a labelled marked value. */
export default function SolanaPositionsTable({ engine }: { engine: string }) {
  const [mode, setMode] = useState("PAPER");
  const { data, error, loading } = useApi<{ positions: Pos[]; note: string }>("/api/analytics/solana-positions",
    { engine: engine || undefined, mode, limit: 50 }, { refreshMs: 60000, reloadOn: ["trade.opened", "trade.closed"] });
  return (
    <Section title="Latest positions" actions={
      <select aria-label="Mode" value={mode} onChange={(e) => setMode(e.target.value)}>
        <option value="PAPER">PAPER</option><option value="LIVE">LIVE</option>
      </select>}>
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {data && data.positions.length === 0 && <Empty>No {mode} positions.</Empty>}
      {data && data.positions.length > 0 && (
        <div className="table-scroll"><table className="data-table">
          <thead><tr><th>Entry</th><th>Token</th><th>Strategy</th><th>Signal</th><th>Size</th><th>Entry price</th>
            <th title="fill vs the price the decision saw; already inside gross and net">Slippage est.</th><th>Exit</th>
            <th>Exit price</th><th>Hold (s)</th><th>Exit reason</th><th>Gross</th><th>Fees</th><th>Net (SOL)</th><th>Net %</th></tr></thead>
          <tbody>{data.positions.map((p) => (
            <tr key={p.id}>
              <td>{formatDate(p.entry_at)}</td>
              <td>{p.mint ? <TokenLink mint={p.mint} label={p.symbol} /> : v(p.symbol)}</td>
              <td>{p.engine}{p.strategy ? ` · ${p.strategy}` : ""}</td>
              <td>{p.signal_at ? formatDate(p.signal_at) : "—"}</td>
              <td>{v(p.size_sol)}</td><td>{v(p.entry_price)}</td><td>{v(p.entry_slippage_estimate_pct)}%</td>
              {p.status === "closed" ? (
                <>
                  <td>{p.exit_at ? formatDate(p.exit_at) : "—"}</td><td>{v(p.exit_price)}</td><td>{v(p.hold_s)}</td>
                  <td>{v(p.exit_reason)}</td><td>{v(p.gross_pnl_sol)}</td><td>{v(p.fees_sol)}</td>
                  <td className={sign(p.net_pnl_sol)}>{v(p.net_pnl_sol)}</td><td>{v(p.net_pnl_pct)}</td>
                </>
              ) : (
                <td colSpan={8} className="muted">{p.status}: marked {v(p.unrealized_marked_sol)} SOL (unrealized, not booked)</td>
              )}
            </tr>))}</tbody>
        </table></div>)}
      {data && <p className="muted small">{data.note}</p>}
    </Section>
  );
}
