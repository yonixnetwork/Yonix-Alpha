"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import TokenIntel from "@/components/TokenIntel";
import TokenTerminal from "@/components/TokenTerminal";
import { Empty, ErrorNotice, Loading, Money, Section, Stat } from "@/components/ui";
import { formatDate, formatDecimal, formatState, gateDecisionPillClass } from "@/lib/format";
import { useApi } from "@/lib/useApi";

export default function TokenDetailPage() {
  const { mint } = useParams<{ mint: string }>();
  const { data, error } = useApi<Record<string, any>>(`/api/tokens/${mint}`, undefined, {
    reloadOn: ["risk.updated", "trade.created", "trade.closed"],
    refreshMs: 30000,
  });
  if (error) return <ErrorNotice error={error} />;
  if (!data) return <Loading />;
  const t = data.token ?? {};
  const meta = data.stream?.meta ?? {};
  const curve = data.stream?.curve;
  const trades: any[] = data.stream?.recent_trades ?? [];
  return (
    <div>
      <TokenTerminal mint={mint} />
      <TokenIntel mint={mint} liveIntel={data?.latest_evidence?.intel ?? null} />
      <details className="term-more">
        <summary>Registry, creator and stream details</summary>
        <div className="stat-grid">
          <Stat label="Name">{t.name || meta.name || "—"}</Stat>
          <Stat label="Creator">
            <span className="mono">{t.creator || meta.creator || "—"}</span>
          </Stat>
          <Stat label="First seen">{formatDate(t.first_seen_at ?? null)}</Stat>
          <Stat label="Curve complete">{curve ? (curve.complete ? "yes (migrated)" : "no") : "—"}</Stat>
          <Stat label="Real SOL in curve">{curve?.rsol !== undefined && curve?.rsol !== null ? formatDecimal(String(curve.rsol / 1e9), 4) : "—"}</Stat>
          <Stat label="Stream trades held">{trades.length}</Stat>
        </div>
      </details>
      <Section title="Safety-gate decisions">
        {data.assessments.length === 0 ? (
          <Empty>Not assessed.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Engine</th>
                  <th>Decision</th>
                  <th>Risk</th>
                  <th>Why</th>
                </tr>
              </thead>
              <tbody>
                {data.assessments.map((a: any) => (
                  <tr key={a.id}>
                    <td>
                      <Link className="link" href={`/dashboard/decisions/${a.id}`}>
                        {formatDate(a.evaluated_at)}
                      </Link>
                    </td>
                    <td className="muted">{a.engine}</td>
                    <td>
                      <span className={gateDecisionPillClass(a.decision)}>{a.decision}</span>
                    </td>
                    <td className={`level-${a.overall_risk}`}>{a.overall_risk}</td>
                    <td className="muted small">{(a.reasons ?? []).slice(0, 2).join("; ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
      <Section title="Paper positions">
        {data.positions.length === 0 ? (
          <Empty>None.</Empty>
        ) : (
          <ul className="reason-list">
            {data.positions.map((p: any) => (
              <li key={p.id}>
                <Link className="link" href={`/dashboard/trades/${p.id}`}>
                  {p.side} {p.status}
                </Link>{" "}
                entry {formatDecimal(p.entry_price, 10)} · PnL <Money value={p.realized_pnl} currency="SOL" digits={6} /> · {p.exit_reason ?? ""}
              </li>
            ))}
          </ul>
        )}
      </Section>
      <Section title="Candidates">
        {data.candidates.length === 0 ? (
          <Empty>None.</Empty>
        ) : (
          <ul className="reason-list">
            {data.candidates.map((c: any) => (
              <li key={c.id}>
                <Link className="link" href={`/dashboard/candidates/${c.id}`}>
                  {c.engine}
                </Link>{" "}
                — {formatState(c.state)} ({formatDate(c.created_at)})
              </li>
            ))}
          </ul>
        )}
      </Section>
      {data.latest_evidence && (
        <details className="term-more">
          <summary>Latest evidence (raw)</summary>
          <pre className="json">{JSON.stringify(data.latest_evidence, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}
