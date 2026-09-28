"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { Crosshair } from "lucide-react";
import ExecutionDiagnostics from "@/components/ExecutionDiagnostics";
import { ErrorNotice, Loading, Money, PageHeader, Section, Stat } from "@/components/ui";
import type { TradeDetail } from "@/lib/cc";
import { formatDate, formatDecimal, formatPct, gateDecisionPillClass } from "@/lib/format";
import { useApi } from "@/lib/useApi";

export default function TradeDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { data, error } = useApi<TradeDetail>(`/api/paper/positions/${id}`, undefined, {
    reloadOn: ["trade.updated", "trade.closed", "position.updated"],
    refreshMs: 20000,
  });
  if (error) return <ErrorNotice error={error} />;
  if (!data) return <Loading />;
  const p = data.position;
  const cur = data.account?.currency;
  const a = data.assessment;
  const venue = (p.plan?.venue ?? {}) as Record<string, unknown>;
  return (
    <div>
      <PageHeader title={`${p.symbol} · ${p.side}`} icon={<Crosshair size={20} aria-hidden />} subtitle={`${data.strategy ?? p.engine} · ${p.status}`}>
        {p.asset_id && String(p.engine ?? "").startsWith("solana") && (
          <Link className="btn btn-ghost btn-sm" href={`/dashboard/tokens/${p.asset_id}`}>
            Token details
          </Link>
        )}
        {a && (
          <Link className="btn btn-ghost btn-sm" href={`/dashboard/decisions/${a.id}`}>
            Entry decision
          </Link>
        )}
      </PageHeader>
      <div className="stat-grid">
        <Stat label="Entry">{formatDecimal(p.entry_price, 10)}</Stat>
        <Stat label={p.status === "open" ? "Last" : "Exit"}>{formatDecimal(p.status === "open" ? p.last_price : p.exit_price, 10)}</Stat>
        <Stat label="Stop loss">{formatDecimal(p.stop_loss, 10)}</Stat>
        <Stat label="Trailing stop">{formatDecimal(p.trailing_stop ?? null, 10)}</Stat>
        <Stat label="Take profits">{(p.take_profit ?? []).map((t: string) => formatDecimal(t, 8)).join(" / ") || "—"}</Stat>
        <Stat label="TP hits">{(p.tp_hits ?? []).map((i: number) => `TP${i + 1}`).join(", ") || "none"}</Stat>
        <Stat label="Size (initial / remaining)">
          {formatDecimal(p.initial_quantity, 6)} / {formatDecimal(p.remaining_quantity, 6)}
        </Stat>
        <Stat label="Entry cost">
          <Money value={p.entry_cost_quote} currency={cur} digits={6} />
        </Stat>
        <Stat label="Max loss at entry">
          <Money value={p.max_loss_quote} currency={cur} digits={6} />
        </Stat>
        <Stat label="Fees">
          <Money value={p.fees_paid_quote} currency={cur} digits={6} />
        </Stat>
        <Stat label="Realized PnL">
          <Money value={p.realized_pnl} currency={cur} digits={6} /> {p.realized_pnl_pct && <span className="muted">{formatPct(p.realized_pnl_pct)}</span>}
        </Stat>
        <Stat label="Exit reason">{p.exit_reason ?? "—"}</Stat>
        <Stat label="Opened">{formatDate(p.entry_at)}</Stat>
        <Stat label="Closed">{formatDate(p.exit_at)}</Stat>
        <Stat label="Venue">{String(venue.venue ?? venue.type ?? "—")}</Stat>
        <Stat label="Execution">
          <span className={p.execution_mode === "LIVE" ? "pill pill-danger" : "pill pill-ok"}>{p.execution_mode ?? "PAPER"}</span>{" "}
          <span className="muted">{p.execution_provider ?? "—"}</span>
        </Stat>
        <Stat label="Source / lifecycle">
          {p.source ?? "—"} / {p.lifecycle ?? "—"}
        </Stat>
        <Stat label="Route / pool">
          {p.execution_route ?? "—"}
          {p.pool ? <span className="mono muted"> {String(p.pool).slice(0, 8)}…</span> : null}
        </Stat>
        <Stat label="Strategy / model / features">
          {p.strategy ?? "—"} / {p.model_version ?? "no model"} / {p.feature_version ?? "—"}
        </Stat>
        <Stat label="Management">{p.management_paused ? "paused (stop still enforced)" : p.exit_requested ? "exit requested" : "active"}</Stat>
      </div>
      {a && (
        <Section title="Entry decision">
          <div className="card">
            <span className={gateDecisionPillClass(a.decision)}>{a.decision}</span> <span className="muted">{a.status_label}</span> · risk{" "}
            <span className={`level-${a.overall_risk}`}>{a.overall_risk}</span>
            <ul className="reason-list">
              {(a.reasons ?? []).map((r: string, i: number) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
            {a.ml && (
              <div className="muted" style={{ marginTop: 8 }}>
                ML: {a.ml.status}
                {a.ml.score !== undefined && ` · score ${Number(a.ml.score).toFixed(3)} (${a.ml.model} v${a.ml.version})`}
                {a.ml.influenced ? " · influenced the decision" : ""}
              </div>
            )}
          </div>
        </Section>
      )}
      {(data.orders ?? []).length > 0 && (
        <Section title="On-chain orders">
          <table className="data-table">
            <thead>
              <tr>
                <th>Side</th>
                <th>Reason</th>
                <th>Status</th>
                <th>Amount</th>
                <th>Fill (SOL / tokens raw)</th>
                <th>Transaction</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {(data.orders ?? []).map((o) => (
                <tr key={o.id}>
                  <td>{o.side}</td>
                  <td>{o.reason}</td>
                  <td>
                    <span className={o.status === "CONFIRMED" ? "pill pill-ok" : o.status === "PENDING" || o.status === "SIGNED" || o.status === "SUBMITTED" ? "pill pill-warn" : "pill pill-danger"}>
                      {o.status}
                    </span>
                    {o.error && <div className="muted">{o.error}</div>}
                  </td>
                  <td>
                    {o.amount} {o.amount_kind}
                  </td>
                  <td className="mono">
                    {o.fill ? `${(o.fill.sol_change_lamports / 1e9).toFixed(6)} / ${o.fill.token_change_raw}` : "— (no fill)"}
                  </td>
                  <td className="mono">
                    {o.signature ? (
                      <a href={`https://solscan.io/tx/${o.signature}`} target="_blank" rel="noreferrer">
                        {o.signature.slice(0, 10)}…
                      </a>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>{formatDate(o.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      )}
      {(data.orders ?? []).some((o) => o.diagnostics) && (
        <Section title="Execution latency & price">
          {(data.orders ?? []).map((o) => <ExecutionDiagnostics key={o.id} order={o} />)}
        </Section>
      )}
      <Section title="Timeline">
        {data.timeline.length === 0 ? (
          <div className="muted">No events.</div>
        ) : (
          <ol className="timeline">
            {data.timeline.map((t, i) => (
              <li key={i}>
                <b>{t.type.replace(/_/g, " ")}</b> <span className="muted">{formatDate(t.at)}</span>
                {t.detail && <div className="mono muted">{JSON.stringify(t.detail)}</div>}
              </li>
            ))}
          </ol>
        )}
      </Section>
    </div>
  );
}
