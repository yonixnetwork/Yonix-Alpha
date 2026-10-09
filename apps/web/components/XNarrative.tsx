"use client";

import { useEffect, useState } from "react";
import RuntimeApply from "@/components/RuntimeApply";
import { ErrorNotice, Section, Stat } from "@/components/ui";
import { apiPut, ApiError } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;

const PILL: Record<string, string> = {
  OK: "pill pill-ok", ENABLED: "pill pill-ok", NO_MATCH: "pill pill-off", NO_DATA: "pill pill-off", "NO X DATA": "pill pill-off",
  NOT_CONFIGURED: "pill pill-off", DISABLED: "pill pill-off", RATE_LIMITED: "pill pill-warn", TIMEOUT: "pill pill-warn",
  UNAVAILABLE: "pill pill-warn", MALFORMED: "pill pill-warn", UNAUTHORIZED: "pill pill-danger", BUDGET_EXHAUSTED: "pill pill-warn",
};

/** Compact X narrative panel for a Solana token (SHADOW: never changes a decision). */
export default function XNarrative({ mint }: { mint: string }) {
  const { data, error } = useApi<J>(`/api/x-narrative/tokens/${mint}`, undefined, { refreshMs: 60000 });
  if (error) return <ErrorNotice error={error} />;
  if (!data) return null;
  const o: J | undefined = data.observations?.[0];
  const f: J = o?.features ?? {};
  return (
    <Section title="X narrative (shadow)">
      <div className="stat-grid">
        <Stat label="Provider"><span className={PILL[data.provider_status] ?? "pill pill-off"}>{data.provider_status}</span></Stat>
        <Stat label="State"><span className={PILL[data.state] ?? "pill pill-off"}>{data.state}</span></Stat>
        {o && <>
          <Stat label="Identity confidence">{o.identity_confidence ?? "—"}</Stat>
          <Stat label="Narrative score">{o.narrative_score ?? <span className="muted">{o.social_data_quality ?? "—"}</span>}</Stat>
          <Stat label="Distinct authors">{f.distinct_authors ?? "—"}</Stat>
          <Stat label="Authors last 5 min / 1 h">{f.windows ? `${f.windows["300s"]?.authors ?? 0} / ${f.windows["3600s"]?.authors ?? 0}` : "—"}</Stat>
          <Stat label="Engagement">{f.engagement_total ?? <span className="muted small">{f.engagement_note ?? "—"}</span>}</Stat>
          <Stat label="Source credibility">{f.source_credibility ?? "—"}</Stat>
          <Stat label="First relevant post">{f.first_relevant_post_at ? formatDate(f.first_relevant_post_at) : "—"}</Stat>
          <Stat label="Looked up">{formatDate(o.observed_at)}</Stat>
        </>}
      </div>
      {o && (f.coordination_flags ?? []).length > 0 && <p className="small neg">Coordination flags: {f.coordination_flags.join(", ")}</p>}
      {o?.features?.reason && <p className="small muted">{o.features.reason}</p>}
      {o && (o.evidence ?? []).length > 0 && (
        <ul className="small">
          {o.evidence.slice(0, 5).map((e: J) => (
            <li key={e.id}>
              <a href={e.link} target="_blank" rel="noreferrer noopener">post {e.id}</a> · {e.evidence} ({e.confidence}) · {e.kind}
              {e.notes?.length ? ` — ${e.notes.join("; ")}` : ""}
            </li>
          ))}
        </ul>
      )}
      <p className="muted small">{data.note}</p>
    </Section>
  );
}

/** Status, budget and settings (Data Providers page). The bearer token is set in .env only and never shown. */
export function XNarrativeSettings() {
  const { data, error, reload } = useApi<J>("/api/x-narrative/status", undefined, { refreshMs: 60000 });
  const [draft, setDraft] = useState<J | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => { if (data) setDraft(data.settings); }, [data]);
  if (error) return <ErrorNotice error={error} />;
  if (!data || !draft) return null;
  async function save() {
    setSaveError(null);
    setSaved(false);
    try {
      await apiPut("/api/x-narrative/settings", draft);
      setSaved(true);
      reload();
    } catch (err) {
      setSaveError(err instanceof ApiError ? err.message : "Save failed.");
    }
  }
  const num = (k: string, label: string, hint?: string) => (
    <div className="form-row">
      <label htmlFor={`xn-${k}`}>{label}</label>
      <input id={`xn-${k}`} inputMode="decimal" value={String(draft[k] ?? "")} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />
      {hint && <div className="form-hint">{hint}</div>}
    </div>
  );
  return (
    <div className="card">
      <p className="muted">
        Optional narrative evidence from the official X API (recent search, pay-per-use, ${data.cost_per_post_usd} per post returned).
        {" "}{data.mode}. Needs X_NARRATIVE_ENABLED=true and X_API_BEARER_TOKEN in .env, and the switch below.
      </p>
      <div className="stat-grid">
        <Stat label="Status"><span className={PILL[data.provider_status] ?? "pill pill-off"}>{data.provider_status}</span></Stat>
        <Stat label="Spent today (USD)">{data.spent_today_usd} / {data.settings.max_daily_budget_usd}</Stat>
        <Stat label="Queued candidates">{data.queued}</Stat>
        <Stat label="Pause">{data.cooldown ?? "none"}</Stat>
      </div>
      <div className="form-grid" style={{ marginTop: 12 }}>
        <div className="form-row">
          <label htmlFor="xn-enabled">Collect X narrative data</label>
          <select id="xn-enabled" value={String(draft.enabled)} onChange={(e) => setDraft({ ...draft, enabled: e.target.value === "true" })}>
            <option value="false">off</option>
            <option value="true">on (shadow only)</option>
          </select>
        </div>
        <div className="form-row">
          <label htmlFor="xn-scope">Which candidates</label>
          <select id="xn-scope" value={draft.query_scope} onChange={(e) => setDraft({ ...draft, query_scope: e.target.value })}>
            <option value="qualified">only tokens whose on-chain signal qualified (cheapest)</option>
            <option value="all">every evaluated token (expensive)</option>
          </select>
        </div>
        {num("max_daily_budget_usd", "Daily budget (USD)", "Lookups stop for the day when it is reached.")}
        {num("max_posts_per_query", "Posts per lookup (10-100)")}
        {num("max_requests_per_token", "Lookups per token per day (1-10)")}
        {num("max_query_age_seconds", "Only posts from the last N seconds")}
        {num("cache_ttl_seconds", "Cache a result for N seconds")}
      </div>
      <ErrorNotice error={saveError} />
      {saved && <div><RuntimeApply inline /></div>}
      <div className="btn-row" style={{ marginTop: 12 }}>
        <button className="btn btn-sm" onClick={save}>Save</button>
      </div>
    </div>
  );
}
