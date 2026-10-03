"use client";

import { ErrorNotice, Loading, Section } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;

const CHAIN: Record<string, string> = { bsc: "BSC", robinhood: "Robinhood Chain" };
const GAP_CLASS: Record<string, string> = { DONE: "pill pill-ok", PENDING: "pill pill-warn", EXPIRED: "pill pill-off", FAILED: "pill pill-danger" };
const idle = (s: number) => (s < 120 ? `${s} s ago` : s < 7200 ? `${Math.round(s / 60)} min ago` : `${(s / 3600).toFixed(1)} h ago`);

/** Master §68-70: how launches and trades are detected per EVM chain, and
 *  the block ranges the live scan skipped with their backfill. */
export default function EvmDetection() {
  const { data, error, loading } = useApi<J>("/api/evm/detection", undefined, { refreshMs: 60000 });
  return (
    <Section title="Detection integrity (BSC, Robinhood Chain)">
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {data && (
        <>
          <p className="muted small">{data.fallback}.</p>
          {Object.entries(data.chains as Record<string, J>).map(([chain, c]) => {
            const [logs, stream, backfill] = c.methods as J[];
            return (
              <div key={chain} style={{ marginBottom: 12 }}>
                <h4 className="small">{CHAIN[chain] ?? chain}</h4>
                <div className="table-scroll"><table className="data-table">
                  <thead><tr><th>Method</th><th>Role</th><th>State</th></tr></thead>
                  <tbody>
                    <tr><td>{logs.method}</td><td className="small">{logs.role}</td>
                      <td className="small">{(logs.launchpads as J[]).length === 0 ? <span className="muted">no cursor yet</span> :
                        (logs.launchpads as J[]).map((l) => (
                          <div key={l.launchpad} title={`advanced ${formatDate(l.advanced_at)}`}>
                            {l.launchpad}: block {Number(l.last_block).toLocaleString()}, {idle(l.idle_s)}
                            {l.idle_s > 300 ? <span className="pill pill-warn"> NOT ADVANCING</span> : null}</div>))}</td></tr>
                    <tr><td>{stream.method}</td><td className="small">{stream.role}</td>
                      <td><span className={stream.state === "CONNECTED" ? "pill pill-ok" : "pill pill-off"}>{stream.state.replaceAll("_", " ")}</span></td></tr>
                    <tr><td>{backfill.method}</td><td className="small">{backfill.role}</td>
                      <td className="small">{Object.keys(backfill.gaps).length === 0 ? <span className="muted">no range skipped</span> :
                        Object.entries(backfill.gaps as Record<string, Record<string, J>>).map(([lp, st]) => (
                          <div key={lp}>{lp}: {Object.entries(st).map(([status, v]) => (
                            <span key={status} style={{ marginRight: 6 }}><span className={GAP_CLASS[status] ?? "pill pill-off"}>{status}</span>{" "}
                              {v.ranges} range(s), {v.blocks_backfilled.toLocaleString()} / {v.blocks.toLocaleString()} blocks,
                              {" "}{v.launches_recovered} launches, {v.trades_recovered} trades recovered</span>))}</div>))}</td></tr>
                  </tbody>
                </table></div>
              </div>
            );
          })}
          {(data.recent_gaps as J[]).length > 0 && (
            <div className="table-scroll"><table className="data-table">
              <thead><tr><th>Skipped</th><th>Chain</th><th>Launchpad</th><th>Blocks</th><th>Status</th><th>Recovered</th><th>Last error</th></tr></thead>
              <tbody>{(data.recent_gaps as J[]).map((g) => (
                <tr key={g.id}>
                  <td>{formatDate(g.detected_at)}</td><td>{CHAIN[g.chain] ?? g.chain}</td><td>{g.launchpad}</td>
                  <td className="mono small">{g.from}–{g.to}{g.status === "PENDING" ? ` (at ${g.next})` : ""}</td>
                  <td><span className={GAP_CLASS[g.status] ?? "pill pill-off"}>{g.status}</span></td>
                  <td className="small">{g.launches} launches, {g.trades} trades</td>
                  <td className="small muted">{g.last_error ?? "—"}</td>
                </tr>))}</tbody>
            </table></div>)}
        </>
      )}
    </Section>
  );
}
