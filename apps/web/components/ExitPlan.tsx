"use client";

import { ErrorNotice, Section, Stat } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;

const STATE_CLASS: Record<string, string> = { SELLABLE: "pill pill-ok", DUST_UNSELLABLE: "pill pill-warn", NOT_VERIFIED: "pill pill-off" };

/** Sellable-amount protection: what each remaining take-profit will sell (raw units, fraction of the ORIGINAL
 * quantity), what the trailing stop / stop loss is left with, and every exit-plan change. */
export default function ExitPlan({ positionId }: { positionId: string }) {
  const { data, error } = useApi<J>(`/api/paper/positions/${positionId}/exit-plan`, undefined,
    { reloadOn: ["trade.updated", "trade.closed"], refreshMs: 30000 });
  if (error) return <ErrorNotice error={error} />;
  if (!data) return null;
  const levels: J[] = data.schedule?.levels ?? [];
  return (
    <Section title="Exit plan">
      <p className="muted small">
        {data.schedule?.basis ?? data.note}. Protection mode <b>{data.settings?.mode}</b>
        {data.settings?.mode === "PAPER" && " (LIVE exits are checked and recorded, not changed)"}. {data.note}
      </p>
      <div className="stat-grid">
        <Stat label="State"><span className={STATE_CLASS[data.state] ?? "pill pill-off"}>{data.state}</span></Stat>
        <Stat label="Original (raw)">{data.initial_raw ?? "—"} <span className="muted small">({data.initial_tokens ?? "—"})</span></Stat>
        <Stat label="Remaining (raw)">{data.remaining_raw ?? "—"} <span className="muted small">({data.remaining_tokens ?? "—"})</span></Stat>
        <Stat label="Sold (raw)">{data.sold_raw ?? "—"}</Stat>
        <Stat label="Route">{data.route ?? "—"}</Stat>
        <Stat label="Sell fee (SOL)">{data.sell_fee_sol}</Stat>
        <Stat label="Price used">{data.price_sol ?? "—"} <span className="muted small">{data.price_at ? formatDate(data.price_at) : ""}</span></Stat>
        <Stat label="Left for trailing / stop (raw)">{data.schedule?.left_for_trailing_or_stop?.raw ?? "—"}</Stat>
      </div>
      {levels.length > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <thead><tr><th>Level</th><th>Price</th><th>Of original</th><th>Status</th><th>Sells (raw)</th><th>Check</th></tr></thead>
            <tbody>
              {levels.map((l) => (
                <tr key={l.level}>
                  <td>TP{l.level}</td><td className="mono small">{l.price}</td><td>{l.fraction_of_original}</td>
                  <td>{l.status}</td><td>{l.sell_raw ?? "—"}</td>
                  <td className="small">{l.action ? `${l.action}${l.reason && l.action !== "SELL" ? ` — ${l.reason}` : ""}` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data.last_failed_exit && (
        <p className="small neg">Last failed exit {formatDate(data.last_failed_exit.at)} ({data.last_failed_exit.status}): {data.last_failed_exit.error}</p>
      )}
      {(data.changes ?? []).length > 0 && (
        <details>
          <summary className="small">Exit-plan changes ({data.changes.length})</summary>
          <ul className="small">
            {data.changes.map((c: J, i: number) => (
              <li key={i}>{formatDate(c.at)} · {c.detail?.exit_reason} · {c.detail?.action}{c.detail?.applied === false ? " (recorded, not applied)" : ""}: sells {c.detail?.sell_raw} raw — {c.detail?.reason}</li>
            ))}
          </ul>
        </details>
      )}
    </Section>
  );
}
