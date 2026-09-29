"use client";

import { formatSol, formatUsdCompact } from "@/lib/format";
import { useSolUsd } from "@/lib/solUsd";

/** A SOL-denominated market cap (or liquidity) shown in USD first, SOL second.
 * `historical`: the SOL value is from the past but converted at the current
 * SOL/USD rate — the tooltip says so. Without a fresh rate: SOL only. */
export default function MarketCap({ sol, historical = false, compact = false }: {
  sol: string | number | null | undefined; historical?: boolean; compact?: boolean;
}) {
  const rate = useSolUsd();
  if (sol === null || sol === undefined || sol === "" || !Number.isFinite(Number(sol))) return <span className="muted">—</span>;
  const s = Number(sol);
  if (rate.price === null) {
    return (
      <span title="USD unavailable: no fresh SOL/USD rate">
        {formatSol(s)}
        {!compact && <span className="muted small"> · USD unavailable</span>}
      </span>
    );
  }
  const note = `${formatSol(s, 4)} × SOL/USD ${rate.price.toFixed(2)} (${rate.source ?? "backend"})` +
    (historical ? "; converted at the current rate, not the rate at that time" : "");
  return (
    <span title={note}>
      {formatUsdCompact(s * rate.price)}
      <span className="muted small"> {formatSol(s)}</span>
    </span>
  );
}
