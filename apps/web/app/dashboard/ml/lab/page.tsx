"use client";

import { useState } from "react";
import { GraduationCap } from "lucide-react";
import { Empty, ErrorNotice, Loading, PageHeader, Pct, Section, Stat, TokenLink } from "@/components/ui";
import { ApiError, apiFetch, apiPost, apiPut } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

interface QueueRow {
  id: string; mint: string; symbol: string | null; category: string; stage: string; decision: string; traded: boolean;
  status: string; decided_at: string; peak_pct: number | null; drawdown_pct: number | null;
  executable_return_pct: number | null; labels: number;
  data_quality: { features_present: number; features_total: number; price_known: boolean };
}
interface Verification { status: string; evidence: string }
interface LabelRow {
  id: string; label: string; text: string; value: number | null; notes: string | null; created_by: string | null;
  created_at: string | null; feature_version: string | null; model_version: string | null; source: string;
  verification: Verification;
}
interface PathPoint {
  horizon: string; change_pct: number | null; peak_so_far_pct: number | null; drawdown_so_far_pct: number | null;
  executable_return_pct: number | null; unknown: string | null;
}
interface Timeline extends Omit<QueueRow, "labels" | "data_quality"> {
  reasons: unknown; decision_time: Record<string, unknown>; feature_version: string; path: PathPoint[];
  labels: LabelRow[]; data_quality: QueueRow["data_quality"] & { missing: string[] };
}
interface Catalog { label: string; text: string; verified_by: string | null }
interface LabelsSummary { catalog: Catalog[]; labels: Record<string, Record<string, number | string | null>>; note: string }
interface Condition { feature: string; op: string; value: number; words?: string | null }
interface Proposal {
  hypothesis: string; categories: string[]; entry_conditions: Condition[]; rejection_conditions: string[];
  exit: { horizon: string; target_pct: number; invalidation_pct: number }; risk_limits: string; required_data: string[];
  evaluation_window: string; success_metrics: string[]; failure_criteria: string[]; comparison: string; testable: boolean;
  untestable_reasons: string[]; plain_language: string[];
}
interface Experiment {
  id: string; idea: string; status: string; testable: boolean; proposal: Proposal; created_by: string | null;
  created_at: string; decided_by: string | null; decided_at: string | null; signals: number; evaluated: number;
}
interface Metrics { n: number; mean?: number; win_rate?: number; profit_factor?: number | null; sum?: number; sum_without_best?: number; max_drawdown_pct_points?: number }
interface Report {
  signals: number; evaluated: number; direction_hit_rate: number | null; executable_rate: number | null; all: Metrics;
  earlier_half: Metrics; later_half: Metrics; first_touched: Record<string, number>; median_mfe_pct: number | null;
  median_mae_pct: number | null; would_fail: number; comparison: { group: string; all: Metrics; later_half: Metrics };
  verdict: { state: string; reason: string };
  recent: { id: string; mint: string; category: string; decided_at: string; horizon: string;
            outcome: { executable_return_pct: number | null; first: string; would_fail: string | null } | null }[];
}
interface Features { features: string[]; ops: string[]; horizons: string[]; categories: string[] }

const VERIFY_CLASS: Record<string, string> = {
  AGREES: "pill pill-ok", DISAGREES: "pill pill-danger", PENDING: "pill pill-warn", OPINION_ONLY: "pill pill-off",
};
const STATUS_CLASS: Record<string, string> = {
  DRAFT: "pill pill-warn", ACCEPTED: "pill pill-ok", REJECTED: "pill pill-off", RETIRED: "pill pill-off",
};
const VERDICT_CLASS: Record<string, string> = {
  INSUFFICIENT_DATA: "pill pill-warn", NOT_SUPPORTED: "pill pill-danger", SUPPORTED_FORWARD: "pill pill-ok",
};

function message(e: unknown): string {
  return e instanceof ApiError || e instanceof Error ? e.message : String(e);
}

function fmtMetrics(m: Metrics | undefined): string {
  if (!m || !m.n) return "n=0";
  const pf = m.profit_factor == null ? "-" : m.profit_factor.toFixed(2);
  return `n=${m.n} · mean ${m.mean?.toFixed(2)} % · win ${((m.win_rate ?? 0) * 100).toFixed(0)} % · PF ${pf} · ` +
    `sum ${m.sum?.toFixed(1)} · without best ${m.sum_without_best?.toFixed(1)} · max drawdown ${m.max_drawdown_pct_points?.toFixed(1)}`;
}

/** Observation queue: what the system saw, traded or not. */
function Queue({ onOpen }: { onOpen: (id: string) => void }) {
  const [hours, setHours] = useState(6);
  const [category, setCategory] = useState("");
  const { data, error, loading } = useApi<{ rows: QueueRow[]; note: string }>("/api/ml-lab/queue",
    { hours, category: category || undefined, limit: 100 }, { refreshMs: 60000 });
  return (
    <Section title="Observation queue" actions={
      <span style={{ display: "flex", gap: 8 }}>
        <select aria-label="Category" value={category} onChange={(e) => setCategory(e.target.value)}>
          <option value="">all categories</option>
          {["FRESH", "MIGRATED", "MOMENTUM"].map((c) => <option key={c} value={c}>{c.toLowerCase()}</option>)}
        </select>
        <select aria-label="Window" value={hours} onChange={(e) => setHours(Number(e.target.value))}>
          {[1, 6, 24, 72].map((h) => <option key={h} value={h}>last {h} h</option>)}
        </select>
      </span>}>
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {data && <p className="muted">{data.note}</p>}
      {data && data.rows.length === 0 && <Empty>No observations in this window.</Empty>}
      {data && data.rows.length > 0 && (
        <div className="table-scroll"><table className="data-table">
          <thead><tr><th>Decided</th><th>Token</th><th>Category</th><th>Stage / decision</th><th>Traded</th>
            <th>Data</th><th>Exec. T+5m</th><th>Peak</th><th>Status</th><th>Labels</th><th /></tr></thead>
          <tbody>{data.rows.map((r) => (
            <tr key={r.id}>
              <td>{formatDate(r.decided_at)}</td><td><TokenLink mint={r.mint} label={r.symbol} /></td><td>{r.category}</td>
              <td>{r.stage} / {r.decision}</td><td>{r.traded ? "yes" : "no"}</td>
              <td title="decision-time features present">{r.data_quality.features_present}/{r.data_quality.features_total}</td>
              <td><Pct value={r.executable_return_pct} /></td><td><Pct value={r.peak_pct} /></td><td>{r.status}</td>
              <td>{r.labels}</td><td><button className="btn btn-sm" onClick={() => onOpen(r.id)}>Open</button></td>
            </tr>))}</tbody>
        </table></div>)}
    </Section>
  );
}

/** One observation: decision-time data, price path, labels; and the label form. */
function Observation({ id, catalog }: { id: string; catalog: Catalog[] }) {
  const { data, error, loading, reload } = useApi<Timeline>(`/api/ml-lab/observations/${id}`);
  const [label, setLabel] = useState("");
  const [value, setValue] = useState("");
  const [notes, setNotes] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const save = async () => {
    setMsg(null);
    try {
      await apiPost(`/api/ml-lab/observations/${id}/labels`, { label, value: value === "" ? null : Number(value), notes: notes || null });
      setLabel(""); setValue(""); setNotes(""); setMsg("Label saved.");
      reload();
    } catch (e) { setMsg(message(e)); }
  };
  return (
    <Section title={`Observation ${data ? `${data.symbol ?? data.mint.slice(0, 8)} · ${data.category} · ${data.stage}` : ""}`}>
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {data && (
        <>
          <div className="stat-grid">
            <Stat label="Decided">{formatDate(data.decided_at)}</Stat>
            <Stat label="Decision">{data.decision}{data.traded ? " (traded)" : ""}</Stat>
            <Stat label="Ledger status">{data.status}</Stat>
            <Stat label="Decision-time features" hint={data.data_quality.missing.length ? `missing: ${data.data_quality.missing.join(", ")}` : undefined}>
              {data.data_quality.features_present}/{data.data_quality.features_total}</Stat>
          </div>
          <h4>At the decision</h4>
          <p className="muted">{Object.entries(data.decision_time).filter(([, v]) => v != null).map(([k, v]) => `${k.replace(/_/g, " ")} ${String(v)}`).join(" · ") || "nothing recorded"}</p>
          <h4>What happened after (recorded outcome)</h4>
          {data.path.length === 0 ? <Empty>No price path recorded yet.</Empty> : (
            <div className="table-scroll"><table className="data-table">
              <thead><tr><th>Horizon</th><th>Price change</th><th>Peak so far</th><th>Drawdown so far</th><th>Executable return</th><th>Note</th></tr></thead>
              <tbody>{data.path.map((p) => (
                <tr key={p.horizon}><td>{p.horizon}</td><td><Pct value={p.change_pct} /></td><td><Pct value={p.peak_so_far_pct} /></td>
                  <td><Pct value={p.drawdown_so_far_pct} /></td><td><Pct value={p.executable_return_pct} /></td><td>{p.unknown ?? ""}</td></tr>))}</tbody>
            </table></div>)}
          <h4>Labels</h4>
          {data.labels.length === 0 ? <Empty>No labels yet.</Empty> : (
            <div className="table-scroll"><table className="data-table">
              <thead><tr><th>Label</th><th>Value</th><th>Notes</th><th>By</th><th>When</th><th>Recorded outcome</th></tr></thead>
              <tbody>{data.labels.map((l) => (
                <tr key={l.id}><td>{l.text}</td><td>{l.value ?? ""}</td><td>{l.notes ?? ""}</td><td>{l.created_by ?? ""}</td>
                  <td>{l.created_at ? formatDate(l.created_at) : ""}</td>
                  <td><span className={VERIFY_CLASS[l.verification.status] ?? "pill"} title={l.verification.evidence}>{l.verification.status.replace("_", " ").toLowerCase()}</span></td></tr>))}</tbody>
            </table></div>)}
          <div className="form-row" style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "end", marginTop: 12 }}>
            <label>Label<br />
              <select value={label} onChange={(e) => setLabel(e.target.value)}>
                <option value="">choose…</option>
                {catalog.map((c) => <option key={c.label} value={c.label}>{c.text}</option>)}
              </select></label>
            <label>Value (optional)<br /><input type="number" value={value} onChange={(e) => setValue(e.target.value)} style={{ width: 110 }} /></label>
            <label style={{ flex: 1, minWidth: 200 }}>Notes (optional)<br /><input value={notes} maxLength={2000} onChange={(e) => setNotes(e.target.value)} style={{ width: "100%" }} /></label>
            <button className="btn" disabled={!label} onClick={save}>Save label</button>
          </div>
          {msg && <p className="muted">{msg}</p>}
          <p className="muted">A label is your opinion. It is stored with the decision-time features ({data.feature_version}) and is never
            changed by the outcome; the last column only compares it with what the market recorded.</p>
        </>)}
    </Section>
  );
}

function ProposalView({ p }: { p: Proposal }) {
  return (
    <div>
      <p><b>Hypothesis:</b> {p.hypothesis}</p>
      <p><b>Categories:</b> {p.categories.join(", ")} · <b>Horizon:</b> {p.exit.horizon} · <b>Target / invalidation:</b> +{p.exit.target_pct} % / -{p.exit.invalidation_pct} %</p>
      <p><b>Entry conditions (decision-time data only):</b></p>
      <ul>{p.entry_conditions.length ? p.entry_conditions.map((c, i) => <li key={i}>{p.plain_language[i]} <span className="muted">({c.feature} {c.op} {c.value})</span></li>) : <li>none</li>}</ul>
      <p><b>Never:</b></p><ul>{p.rejection_conditions.map((r) => <li key={r}>{r}</li>)}</ul>
      <p><b>Risk:</b> {p.risk_limits}</p>
      <p><b>Compared with:</b> {p.comparison} · <b>Evaluation:</b> {p.evaluation_window}</p>
      <p><b>Success:</b> {p.success_metrics.join("; ")}</p>
      <p><b>Failure:</b> {p.failure_criteria.join("; ")}</p>
      {!p.testable && <p className="pill pill-danger">Not testable: {p.untestable_reasons.join("; ")}</p>}
    </div>
  );
}

function ConditionEditor({ exp, features, onSaved }: { exp: Experiment; features: Features; onSaved: () => void }) {
  const [conds, setConds] = useState<Condition[]>(exp.proposal.entry_conditions);
  const [horizon, setHorizon] = useState(exp.proposal.exit.horizon);
  const [cats, setCats] = useState<string[]>(exp.proposal.categories);
  const [msg, setMsg] = useState<string | null>(null);
  const set = (i: number, patch: Partial<Condition>) => setConds(conds.map((c, j) => (j === i ? { ...c, ...patch, words: patch.value === undefined ? c.words : null } : c)));
  const save = async () => {
    setMsg(null);
    try {
      await apiPut(`/api/ml-lab/experiments/${exp.id}`, {
        entry_conditions: conds, categories: cats,
        exit: { horizon, target_pct: exp.proposal.exit.target_pct, invalidation_pct: exp.proposal.exit.invalidation_pct },
      });
      onSaved();
    } catch (e) { setMsg(message(e)); }
  };
  return (
    <div style={{ borderTop: "1px solid var(--border, #333)", paddingTop: 8 }}>
      <b>Edit before accepting</b>
      {conds.map((c, i) => (
        <div key={i} style={{ display: "flex", gap: 6, marginTop: 4 }}>
          <select aria-label="Feature" value={c.feature} onChange={(e) => set(i, { feature: e.target.value, words: null })}>
            {features.features.map((f) => <option key={f} value={f}>{f}</option>)}
          </select>
          <select aria-label="Operator" value={c.op} onChange={(e) => set(i, { op: e.target.value, words: null })}>
            {features.ops.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
          <input aria-label="Value" type="number" value={c.value} onChange={(e) => set(i, { value: Number(e.target.value) })} style={{ width: 110 }} />
          <button className="btn btn-sm" onClick={() => setConds(conds.filter((_, j) => j !== i))}>Remove</button>
        </div>))}
      <div style={{ display: "flex", gap: 8, marginTop: 6, flexWrap: "wrap" }}>
        <button className="btn btn-sm" onClick={() => setConds([...conds, { feature: features.features[0], op: ">=", value: 0 }])}>Add condition</button>
        {features.categories.map((c) => (
          <label key={c}><input type="checkbox" checked={cats.includes(c)} onChange={(e) => setCats(e.target.checked ? [...cats, c] : cats.filter((x) => x !== c))} /> {c.toLowerCase()}</label>))}
        <select aria-label="Horizon" value={horizon} onChange={(e) => setHorizon(e.target.value)}>
          {features.horizons.map((h) => <option key={h} value={h}>{h}</option>)}
        </select>
        <button className="btn btn-sm" onClick={save}>Save changes</button>
      </div>
      {msg && <p className="muted">{msg}</p>}
    </div>
  );
}

function ExperimentReport({ id }: { id: string }) {
  const { data, error, loading } = useApi<Report>(`/api/ml-lab/experiments/${id}/report`, undefined, { refreshMs: 120000 });
  if (loading && !data) return <Loading />;
  if (error) return <ErrorNotice error={error} />;
  if (!data) return null;
  return (
    <div>
      <p><span className={VERDICT_CLASS[data.verdict.state] ?? "pill"}>{data.verdict.state.replace(/_/g, " ").toLowerCase()}</span> {data.verdict.reason}</p>
      <div className="stat-grid">
        <Stat label="Signals / evaluated">{data.signals} / {data.evaluated}</Stat>
        <Stat label="Direction hit rate" hint="never sufficient on its own">{data.direction_hit_rate == null ? "-" : `${(data.direction_hit_rate * 100).toFixed(0)} %`}</Stat>
        <Stat label="Executable" hint="a 0.05 SOL round trip existed">{data.executable_rate == null ? "-" : `${(data.executable_rate * 100).toFixed(0)} %`}</Stat>
        <Stat label="Median MFE / MAE"><Pct value={data.median_mfe_pct} /> / <Pct value={data.median_mae_pct} /></Stat>
      </div>
      <p className="muted">Net executable return after costs, in % per reference trade.</p>
      <ul>
        <li>All signals: {fmtMetrics(data.all)}</li>
        <li>Earlier half: {fmtMetrics(data.earlier_half)}</li>
        <li>Later half (judged): {fmtMetrics(data.later_half)}</li>
        <li>Comparison ({data.comparison.group}), later half: {fmtMetrics(data.comparison.later_half)}</li>
        <li>First touched: {Object.entries(data.first_touched).map(([k, v]) => `${k.toLowerCase()} ${v}`).join(", ") || "-"} · could not be executed: {data.would_fail}</li>
      </ul>
      {data.recent.length > 0 && (
        <div className="table-scroll"><table className="data-table">
          <thead><tr><th>Signal time</th><th>Token</th><th>Category</th><th>Horizon</th><th>Executable return</th><th>First</th></tr></thead>
          <tbody>{data.recent.map((s) => (
            <tr key={s.id}><td>{formatDate(s.decided_at)}</td><td><TokenLink mint={s.mint} /></td><td>{s.category}</td><td>{s.horizon}</td>
              <td>{s.outcome ? <Pct value={s.outcome.executable_return_pct} /> : "pending"}</td><td>{s.outcome?.first ?? ""}</td></tr>))}</tbody>
        </table></div>)}
    </div>
  );
}

/** Idea -> proposed experiment -> operator decision -> forward signals. */
function Experiments() {
  const { data, error, loading, reload } = useApi<{ experiments: Experiment[]; note: string }>("/api/ml-lab/experiments", undefined, { refreshMs: 60000 });
  const feats = useApi<Features>("/api/ml-lab/features");
  const [idea, setIdea] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const submit = async () => {
    setMsg(null);
    try {
      const e = await apiPost<Experiment>("/api/ml-lab/ideas", { idea });
      setIdea(""); setOpen(e.id); reload();
    } catch (e) { setMsg(message(e)); }
  };
  const act = async (id: string, what: "accept" | "reject" | "retire") => {
    setMsg(null);
    try { await apiPost(`/api/ml-lab/experiments/${id}/${what}`); reload(); } catch (e) { setMsg(message(e)); }
  };
  return (
    <Section title="Ideas and experiments">
      <p className="muted">Write a trading idea in plain words. It becomes a proposed experiment you can read and edit. Nothing runs until you
        accept it, and an accepted experiment only records predictions for new decisions: it never places an order or changes a strategy.</p>
      <textarea aria-label="Idea" value={idea} onChange={(e) => setIdea(e.target.value)} rows={3} style={{ width: "100%" }} maxLength={4000}
        placeholder="Example: migrated tokens with sustained buying volume, qualified wallets and liquidity above 30 SOL before a large price extension may outperform the existing confirmation strategy" />
      <button className="btn" disabled={idea.trim().length < 10} onClick={submit}>Propose experiment</button>
      {msg && <p className="muted">{msg}</p>}
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {data && data.experiments.length === 0 && <Empty>No experiments yet.</Empty>}
      {data?.experiments.map((e) => (
        <div key={e.id} className="card" style={{ marginTop: 12, padding: 12 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <span className={STATUS_CLASS[e.status] ?? "pill"}>{e.status.toLowerCase()}</span>
            {!e.testable && <span className="pill pill-danger">not testable</span>}
            <b style={{ flex: 1 }}>{e.idea}</b>
            <span className="muted">{e.signals} signals · {e.evaluated} evaluated · {formatDate(e.created_at)}</span>
            <button className="btn btn-sm" onClick={() => setOpen(open === e.id ? null : e.id)}>{open === e.id ? "Close" : "Details"}</button>
          </div>
          {open === e.id && (
            <div style={{ marginTop: 8 }}>
              <ProposalView p={e.proposal} />
              {e.status === "DRAFT" && feats.data && <ConditionEditor key={JSON.stringify(e.proposal)} exp={e} features={feats.data} onSaved={reload} />}
              <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                {e.status === "DRAFT" && <button className="btn" disabled={!e.testable} title={e.testable ? "start recording predictions for new decisions" : "not testable"} onClick={() => act(e.id, "accept")}>Accept and start testing</button>}
                {e.status === "DRAFT" && <button className="btn btn-ghost" onClick={() => act(e.id, "reject")}>Reject</button>}
                {e.status === "ACCEPTED" && <button className="btn btn-ghost" onClick={() => act(e.id, "retire")}>Stop testing</button>}
              </div>
              {e.status !== "DRAFT" && e.status !== "REJECTED" && <ExperimentReport id={e.id} />}
            </div>)}
        </div>))}
    </Section>
  );
}

function LabelSummary({ summary }: { summary: LabelsSummary | null }) {
  if (!summary) return null;
  return (
    <Section title="Your labels against the recorded outcomes">
      <p className="muted">{summary.note}</p>
      <div className="table-scroll"><table className="data-table">
        <thead><tr><th>Label</th><th>How the outcome checks it</th><th>Agrees</th><th>Disagrees</th><th>Pending</th><th>Opinion only</th></tr></thead>
        <tbody>{summary.catalog.map((c) => {
          const s = summary.labels[c.label] ?? {};
          return <tr key={c.label}><td>{c.text}</td><td className="muted">{c.verified_by ?? "not measured by the ledger"}</td>
            <td>{s.AGREES ?? 0}</td><td>{s.DISAGREES ?? 0}</td><td>{s.PENDING ?? 0}</td><td>{s.OPINION_ONLY ?? 0}</td></tr>;
        })}</tbody>
      </table></div>
    </Section>
  );
}

function DatasetExport() {
  const [days, setDays] = useState(7);
  const [msg, setMsg] = useState<string | null>(null);
  const { data } = useApi<{ count: number; duplicates_dropped: number; incomplete_outcomes: number; feature_version: string;
    missing_by_feature: Record<string, number>; leakage_rule: string; truncated: boolean }>("/api/ml-lab/dataset", { days });
  const download = async () => {
    setMsg(null);
    try {
      const res = await apiFetch(`/api/ml-lab/dataset?days=${days}&format=csv`);
      if (!res.ok) throw new Error(`export failed (${res.status})`);
      const url = URL.createObjectURL(await res.blob());
      const a = document.createElement("a");
      a.href = url; a.download = `yonix-dataset-${days}d.csv`; a.click();
      URL.revokeObjectURL(url);
    } catch (e) { setMsg(message(e)); }
  };
  return (
    <Section title="Training dataset export" actions={
      <select aria-label="Days" value={days} onChange={(e) => setDays(Number(e.target.value))}>
        {[1, 3, 7, 14, 30].map((d) => <option key={d} value={d}>last {d} days</option>)}
      </select>}>
      {data && (
        <>
          <p>{data.count} rows (one per decision, {data.duplicates_dropped} duplicates dropped), {data.incomplete_outcomes} with the outcome
            still being measured. Features {data.feature_version}{data.truncated ? " · truncated: choose fewer days" : ""}.</p>
          <p className="muted">{data.leakage_rule}</p>
          {Object.keys(data.missing_by_feature).length > 0 && (
            <p className="muted">Most often missing: {Object.entries(data.missing_by_feature).slice(0, 8).map(([k, v]) => `${k} ${v}`).join(", ")}</p>)}
        </>)}
      <button className="btn" onClick={download}>Download CSV</button>
      {msg && <p className="muted">{msg}</p>}
    </Section>
  );
}

export default function MlLabPage() {
  const [openId, setOpenId] = useState<string | null>(null);
  const labels = useApi<LabelsSummary>("/api/ml-lab/labels", undefined, { refreshMs: 120000 });
  return (
    <>
      <PageHeader title="ML Training Lab" icon={<GraduationCap size={20} />}
        subtitle="Label what the system observed, test your ideas as experiments, export clean training data. Nothing here trades." />
      <Queue onOpen={setOpenId} />
      {openId && <Observation key={openId} id={openId} catalog={labels.data?.catalog ?? []} />}
      <Experiments />
      <LabelSummary summary={labels.data} />
      <DatasetExport />
    </>
  );
}
