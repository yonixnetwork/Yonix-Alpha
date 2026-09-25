"use client";

import { Fragment, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { apiGet, apiPost, ApiError } from "@/lib/api";
import { formatBps, formatDate, formatDecimal, formatPct, gateDecisionPillClass } from "@/lib/format";
import type { AssessmentDetail, PlannedValue } from "@/lib/types";

function Provenance({ v }: { v: PlannedValue | null }) {
  if (!v) return <span className="muted">—</span>;
  return (
    <span>
      {formatDecimal(v.value, 10)}{" "}
      <span className={v.provenance === "MANUAL" ? "pill pill-warn" : "pill pill-off"}>{v.provenance}</span>
      <div className="form-hint">{v.method}</div>
    </span>
  );
}

export default function DecisionDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const [a, setA] = useState<AssessmentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setA(await apiGet<AssessmentDetail>(`/api/control/assessments/${params.id}`));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return router.replace("/login");
      setError(err instanceof ApiError && err.status === 404 ? "Decision not found." : "Failed to load decision.");
    }
  }, [params.id, router]);

  useEffect(() => {
    load();
  }, [load]);

  async function act(action: "approve" | "decline") {
    setNotice(null);
    try {
      await apiPost(`/api/control/assessments/${params.id}/${action}`);
      setNotice(action === "approve" ? "Approved. Every check re-runs on the next evaluation." : "Declined.");
      load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Action failed.");
    }
  }

  if (error) return <div className="error">{error}</div>;
  if (!a) return <div className="empty-state">Loading…</div>;

  const d = a.assessment;
  const plan = d.plan;
  const evidence = d.inputs_snapshot ?? {};
  const errors = (evidence.errors as string[] | undefined) ?? [];
  const findings = [...(d.findings ?? [])].sort((x, y) => Number(y.hard_block) - Number(x.hard_block));

  return (
    <div>
      <div className="page-header">
        <div className="page-title">
          {a.symbol ?? "—"} <span className={gateDecisionPillClass(a.decision)}>{a.decision}</span>
        </div>
        <Link href="/dashboard/decisions" className="btn btn-ghost btn-sm">
          Back
        </Link>
      </div>

      {notice && <div className="success">{notice}</div>}
      {a.approval_state === "PENDING" && (
        <div className="notice notice-warn">
          Waiting for operator approval. Approving lets the next evaluation proceed <b>only if every safety check still
          passes on fresh data</b>.
          <div className="btn-row" style={{ marginTop: 8 }}>
            <button className="btn btn-sm" onClick={() => act("approve")}>
              Approve
            </button>
            <button className="btn btn-ghost btn-sm" onClick={() => act("decline")}>
              Decline
            </button>
          </div>
        </div>
      )}

      <div className="card-grid">
        <div className="card">
          <div className="status-label">Status</div>
          <dl className="kv">
            <dt>Label</dt>
            <dd>{a.status_label}</dd>
            <dt>Engine</dt>
            <dd>{a.engine}</dd>
            <dt>Target</dt>
            <dd>{a.execution_target}</dd>
            <dt>Overall risk</dt>
            <dd className={`level-${a.overall_risk}`}>{a.overall_risk}</dd>
            <dt>Signal qualified</dt>
            <dd>{d.qualified ? "yes" : "no"}</dd>
            <dt>Approval</dt>
            <dd>{a.approval_state}</dd>
            <dt>Evaluated</dt>
            <dd>{formatDate(a.evaluated_at)}</dd>
            <dt>Mint</dt>
            <dd className="mono">{a.asset_id}</dd>
          </dl>
        </div>
        <div className="card">
          <div className="status-label">Data freshness</div>
          <dl className="kv">
            {Object.entries(d.data_status ?? {}).map(([k, v]) => (
              <Fragment key={k}>
                <dt>{k}</dt>
                <dd className={v === "LIVE" ? "level-LOW" : v === "DEGRADED" ? "level-HIGH" : "level-CRITICAL"}>
                  {v}
                </dd>
              </Fragment>
            ))}
          </dl>
          {errors.length > 0 && (
            <>
              <div className="status-label" style={{ marginTop: 12 }}>
                Source errors
              </div>
              <ul className="reason-list">
                {errors.map((e, i) => (
                  <li key={i}>{e}</li>
                ))}
              </ul>
            </>
          )}
        </div>
        <div className="card">
          <div className="status-label">Risk by category</div>
          <dl className="kv">
            {Object.entries(d.category_risk ?? {}).map(([k, v]) => (
              <Fragment key={k}>
                <dt>{k}</dt>
                <dd className={`level-${v}`}>
                  {v}
                </dd>
              </Fragment>
            ))}
          </dl>
        </div>
      </div>

      <div className="section-title">Findings</div>
      <table className="data-table">
        <thead>
          <tr>
            <th>Category</th>
            <th>Level</th>
            <th>Code</th>
            <th>Finding</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          {findings.map((f, i) => (
            <tr key={i}>
              <td>{f.category}</td>
              <td className={`level-${f.level}`}>{f.level}</td>
              <td className="mono">
                {f.code}
                {f.hard_block ? " · hard block" : ""}
              </td>
              <td>{f.message}</td>
              <td>
                <span className={gateDecisionPillClass(f.action)}>{f.action}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="section-title">Trade plan</div>
      {!plan?.entry_price ? (
        <div className="empty-state">No plan: the gate stopped before risk could be defined (see findings).</div>
      ) : (
        <div className="card-grid">
          <div className="card">
            <dl className="kv">
              <dt>Entry (market)</dt>
              <dd>{formatDecimal(plan.entry_price, 12)}</dd>
              <dt>Stop loss</dt>
              <dd>
                <Provenance v={plan.stop_loss} />
              </dd>
              <dt>Stop distance</dt>
              <dd>{formatPct(plan.stop_distance_pct)}</dd>
              <dt>Max loss</dt>
              <dd>
                <Provenance v={plan.max_loss} />
              </dd>
              <dt>Position size</dt>
              <dd>
                <Provenance v={plan.position_size} />
              </dd>
              <dt>Binding cap</dt>
              <dd>{plan.binding_cap ?? "—"}</dd>
              <dt>Entry / exit cost</dt>
              <dd>
                {formatBps(plan.entry_cost_bps)} / {formatBps(plan.exit_cost_bps)}
              </dd>
            </dl>
          </div>
          <div className="card">
            <div className="status-label">Take profits</div>
            <dl className="kv">
              {plan.take_profits.map((tp, i) => (
                <Fragment key={i}>
                  <dt>
                    TP{i + 1} ({formatPct(tp.exit_fraction, 0)})
                  </dt>
                  <dd>
                    <Provenance v={tp.price} />
                  </dd>
                </Fragment>
              ))}
            </dl>
            <div className="status-label" style={{ marginTop: 12 }}>
              Trailing stop
            </div>
            {plan.trailing?.enabled ? (
              <div className="muted">
                {formatPct(plan.trailing.distance_pct)} distance, activates at {formatDecimal(plan.trailing.activation_price, 12)} ·{" "}
                {plan.trailing.provenance} ({plan.trailing.method})
              </div>
            ) : (
              <div className="muted">disabled</div>
            )}
          </div>
        </div>
      )}

      {a.outcome && (
        <>
          <div className="section-title">What happened afterwards</div>
          <div className="notice">
            {"superseded_by" in a.outcome ? (
              <>A later evaluation of this token carries the outcome.</>
            ) : (
              <>
                Price change {a.outcome.change_pct ? formatPct(String(a.outcome.change_pct)) : "unknown"} after{" "}
                {String(a.outcome.after_seconds ?? "?")}s ({String(a.outcome.source ?? "")}
                {a.outcome.graduated ? ", curve graduated" : ""}). Ignores costs and exitability: for reviewing rejections,
                not a profit claim.
              </>
            )}
          </div>
        </>
      )}

      <div className="section-title">Timeline</div>
      {a.timeline.length === 0 ? (
        <div className="muted">No events.</div>
      ) : (
        <ul className="timeline">
          {a.timeline.map((t) => (
            <li key={t.id}>
              <b>{t.event_type}</b> <span className="muted">{formatDate(t.occurred_at)}</span>
              {t.detail && <div className="mono muted">{JSON.stringify(t.detail)}</div>}
            </li>
          ))}
        </ul>
      )}

      <div className="section-title">Evidence (raw inputs)</div>
      <pre className="json">{JSON.stringify(evidence, null, 2)}</pre>
      <div className="section-title">Settings used</div>
      <pre className="json">{JSON.stringify({ versions: d.versions, settings: d.settings_snapshot }, null, 2)}</pre>
    </div>
  );
}
