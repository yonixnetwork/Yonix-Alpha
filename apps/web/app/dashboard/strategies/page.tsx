"use client";

import Link from "next/link";
import { Layers } from "lucide-react";
import { ErrorNotice, Loading, modeClass, Money, PageHeader, Pct } from "@/components/ui";
import type { StrategyOut } from "@/lib/cc";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

const HREF: Record<string, string> = {
  solana_fresh: "/dashboard/solana/fresh",
  solana_migration: "/dashboard/solana/migrated",
  solana_momentum: "/dashboard/solana/momentum",
};

export default function StrategiesPage() {
  const { data, error, loading } = useApi<StrategyOut[]>("/api/strategies", undefined, {
    reloadOn: ["strategy.updated", "trade.closed"],
    refreshMs: 30000,
  });
  return (
    <div>
      <PageHeader title="Strategies" icon={<Layers size={20} aria-hidden />} subtitle="The Solana strategies: mode, status and paper results. BSC and Robinhood Chain are configured under Chains." />
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {data && (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Strategy</th>
                <th>Kind</th>
                <th>Mode (effective)</th>
                <th>Open</th>
                <th>Trades</th>
                <th>Win rate</th>
                <th>Realized PnL</th>
                <th>Last decision</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {data.map((s) => (
                <tr key={s.name}>
                  <td>
                    <Link className="link" href={HREF[s.name] ?? `/dashboard/strategies/${s.name}`}>
                      {s.label}
                    </Link>
                  </td>
                  <td className="muted">{s.kind}</td>
                  <td>
                    {s.mode ? (
                      <>
                        <span className={modeClass(s.mode)}>{s.mode}</span>
                        {s.effective_mode !== s.mode && <span className={modeClass(s.effective_mode)}> → {s.effective_mode}</span>}
                      </>
                    ) : (
                      <span className="muted">n/a</span>
                    )}
                  </td>
                  <td>{s.open_positions ?? "—"}</td>
                  <td>{s.trades ?? "—"}</td>
                  <td>{s.trades !== undefined ? <Pct value={s.win_rate} /> : "—"}</td>
                  <td>{s.total_pnl !== undefined ? <Money value={s.total_pnl} currency={s.currency} /> : "—"}</td>
                  <td className="muted">{s.last_decision_at !== undefined ? formatDate(s.last_decision_at ?? null) : "—"}</td>
                  <td className="muted small">{s.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
