"use client";

import { useState } from "react";
import { Empty, ErrorNotice, Loading, ReviewStatus, Section, Stat, reviewReady } from "@/components/ui";
import { ApiError, apiGet, apiPost } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const v = (x: unknown) => (x === null || x === undefined ? "—" : String(x));
const VERDICT: Record<string, string> = {
  "INSUFFICIENT EVIDENCE": "pill pill-warn", "NO BASELINE": "pill pill-off", INCONCLUSIVE: "pill pill-off",
  IMPROVING: "pill pill-ok", DETERIORATING: "pill pill-danger",
};
const ci = (x: number[] | null) => (x ? `${x[0]} .. ${x[1]}` : "—");

/** Forward test (GET /api/analytics/forward-test, yonixalpha_core.forward_test):
 * a timestamped period with frozen rules, judged against the baseline before it. */
export default function ForwardTestPanel() {
  const q = useApi<J>("/api/analytics/forward-test", undefined, { refreshMs: 120000 });
  const [label, setLabel] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const data = q.data && reviewReady(q.data) ? q.data : null;
  const refresh = () => { void apiGet("/api/analytics/forward-test", { refresh: true }).catch(() => undefined).then(() => q.reload()); };
  const start = async () => {
    setMsg(null);
    try {
      const p = await apiPost<J>("/api/analytics/forward-test/start", { label });
      setLabel(""); setMsg(`Started ${p.id} on revision ${p.deployment?.revision ?? "not recorded"}.`); refresh();
    } catch (e) { setMsg(e instanceof ApiError || e instanceof Error ? e.message : String(e)); }
  };
  const period = data?.period;
  return (
    <Section title="Forward test">
      <p className="muted small">A new evaluation period with fixed rules, started after a verified deployment. Seven days is a first
        observation window, not proof of an edge. Starting a new period ends the current one; both are kept.</p>
      <ErrorNotice error={q.error} />
      {q.loading && !q.data && <Loading />}
      {q.data && !data && <ReviewStatus data={q.data} onRefresh={refresh} />}
      {data?.state === "NOT STARTED" && <Empty>No forward test yet.</Empty>}
      {period && (
        <>
          <div className="stat-grid">
            <Stat label="Period">{period.label} ({data!.state.toLowerCase()})</Stat>
            <Stat label="Started">{formatDate(period.started_at)} by {v(period.started_by)}</Stat>
            <Stat label="Deployed revision">{v(period.deployment?.revision)}</Stat>
            <Stat label="Elapsed days">{data!.elapsed_days}</Stat>
            <Stat label="Data sources at start">{v(period.data_sources?.overall)}</Stat>
            <Stat label="Rules (frozen)">{data!.rules.min_closed_trades} trades · {data!.rules.min_days} days · {data!.rules.min_distinct_tokens} tokens</Stat>
          </div>
          <p className={data!.configuration_changed.length ? "small neg" : "muted small"}>Configuration: {data!.configuration_note}</p>
          <div className="table-scroll"><table className="data-table">
            <thead><tr><th>Strategy</th><th>Mode</th><th>Verdict</th><th>Closed</th><th>Tokens</th><th>Net (SOL)</th>
              <th>Net per trade</th><th>95 % interval</th><th>Baseline per trade (n)</th><th>Difference interval</th><th>Missing evidence</th></tr></thead>
            <tbody>{Object.entries(data!.strategies as J).flatMap(([e, s]) => Object.entries((s as J).modes as J).map(([m, x]) => {
              const j = (x as J).judgement;
              return (
                <tr key={`${e}${m}`}><td>{e}</td><td>{m}</td><td><span className={VERDICT[j.verdict] ?? "pill"}>{j.verdict}</span></td>
                  <td>{j.closed_trades}</td><td>{j.distinct_tokens}</td><td>{v(j.net_sol)}</td><td>{v(j.expectancy_sol)}</td>
                  <td>{ci(j.expectancy_ci_sol)}</td><td>{v(j.baseline_expectancy_sol)} ({j.baseline_trades})</td>
                  <td>{ci(j.difference_ci_sol)}</td><td className="small">{j.evidence_gaps.join("; ") || "—"}</td></tr>);
            }))}</tbody>
          </table></div>
          <ul className="muted small">{(data!.notes as string[]).map((n) => <li key={n}>{n}</li>)}</ul>
        </>
      )}
      <div className="btn-row" style={{ marginTop: 8 }}>
        <input aria-label="Label" value={label} maxLength={120} onChange={(e) => setLabel(e.target.value)}
          placeholder="Label, for example: after deploy abc1234" style={{ minWidth: 260 }} />
        <button className="btn btn-sm" disabled={label.trim().length < 3} onClick={start}>Start a new forward test now</button>
      </div>
      {msg && <p className="muted small">{msg}</p>}
    </Section>
  );
}
