"use client";

import { Play, Square } from "lucide-react";
import ConfirmButton from "@/components/ConfirmDialog";
import { Empty, ErrorNotice, Money, Section, Stat } from "@/components/ui";
import { apiPost } from "@/lib/api";
import type { StrategyOut } from "@/lib/cc";
import { formatDate, formatDecimal } from "@/lib/format";
import { useApi } from "@/lib/useApi";

interface OrderRow {
  id: string;
  symbol: string;
  side: string;
  type: string;
  price: string;
  quantity: string;
  fee: string;
  status: string;
  filled_at: string;
  detail: Record<string, unknown> | null;
}

export default function GridSection({ s, reload }: { s: StrategyOut; reload: () => void }) {
  const orders = useApi<OrderRow[]>("/api/paper/orders", { strategy: "hyperliquid_grid", limit: 50 }, { reloadOn: ["trade.updated"] });
  const sessions = s.grid_sessions ?? [];
  async function command(cmd: "start" | "stop") {
    await apiPost(`/api/strategies/hyperliquid_grid/${cmd}`);
    reload();
  }
  return (
    <>
      <Section
        title="Grid sessions"
        actions={
          <div className="btn-row">
            <ConfirmButton
              label={
                <>
                  <Play size={14} aria-hidden /> Start
                </>
              }
              title="Start the paper grid?"
              body="The paper-trading loop builds the grid around the live Hyperliquid mid on its next tick, but only if the worst-case loss fits max_daily_loss_quote. Capital is reserved from the Hyperliquid paper book."
              onConfirm={() => command("start")}
            />
            <ConfirmButton
              label={
                <>
                  <Square size={14} aria-hidden /> Stop
                </>
              }
              danger
              title="Stop the paper grid?"
              body="Open grid inventory is flattened at the live mid with the taker fee, and capital plus PnL returns to the paper book."
              onConfirm={() => command("stop")}
            />
          </div>
        }
      >
        {sessions.length === 0 && <Empty>No grid has run yet.</Empty>}
        {sessions.map((g) => (
          <div className="card" key={g.coin}>
            <div className="status-label">
              {g.coin} · <span className={g.status === "running" ? "pill pill-ok" : g.status === "refused" ? "pill pill-danger" : "pill pill-off"}>{g.status}</span>
              {g.reason && <span className="muted"> — {g.reason}</span>}
            </div>
            <div className="stat-grid">
              <Stat label="Reserved capital">{formatDecimal(g.reserved ?? null, 4)} USDC</Stat>
              <Stat label="Equity">{formatDecimal(g.equity ?? null, 4)}</Stat>
              <Stat label="Session PnL">
                <Money value={g.pnl} currency="USDC" />
              </Stat>
              <Stat label="Realized / fees">
                <Money value={g.realized_pnl} /> / {formatDecimal(g.fees ?? null, 4)}
              </Stat>
              <Stat label="Fills">{g.fills ?? 0}</Stat>
              <Stat label="Net position">{formatDecimal(g.net_position ?? null, 6)}</Stat>
              <Stat label="Range">
                {formatDecimal(g.range_lower ?? null, 2)} – {formatDecimal(g.range_upper ?? null, 2)} ({g.levels ?? "—"} levels)
              </Stat>
              <Stat label="Drawdown">{g.drawdown_pct ? `${Number(g.drawdown_pct).toFixed(2)}%` : "—"}</Stat>
              <Stat label="Worst-case loss">{formatDecimal(g.worst_case_loss ?? null, 4)}</Stat>
              <Stat label="Last mid">{formatDecimal(g.last_mid ?? null, 4)}</Stat>
            </div>
            {g.paused && <div className="notice notice-warn">Circuit breaker: {g.paused}</div>}
          </div>
        ))}
      </Section>
      <Section title="Grid fills (paper orders)">
        <ErrorNotice error={orders.error} />
        {orders.data && orders.data.length === 0 && <Empty>No fills yet.</Empty>}
        {orders.data && orders.data.length > 0 && (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Coin</th>
                  <th>Side</th>
                  <th>Type</th>
                  <th>Price</th>
                  <th>Size</th>
                  <th>Fee</th>
                </tr>
              </thead>
              <tbody>
                {orders.data.map((o) => (
                  <tr key={o.id}>
                    <td className="muted">{formatDate(o.filled_at)}</td>
                    <td>{o.symbol}</td>
                    <td>
                      <span className={o.side === "BUY" ? "pill pill-ok" : "pill pill-danger"}>{o.side}</span>
                    </td>
                    <td>{o.type}</td>
                    <td>{formatDecimal(o.price, 6)}</td>
                    <td>{formatDecimal(o.quantity, 6)}</td>
                    <td>{formatDecimal(o.fee, 6)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </>
  );
}
