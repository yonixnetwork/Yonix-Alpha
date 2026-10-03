"use client";

import { useState } from "react";
import { RefreshCw } from "lucide-react";
import { Empty, ErrorNotice, Section } from "@/components/ui";
import { apiPost, apiPut } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const NAME: Record<string, string> = { nansen: "Nansen", madeonsol: "MadeOnSol" };
const STATUS_PILL: Record<string, string> = { OK: "pill pill-ok", NOT_CONFIGURED: "pill pill-off", UNSUPPORTED_CHAIN: "pill pill-off",
  BUDGET_EXHAUSTED: "pill pill-warn", RATE_LIMITED: "pill pill-warn", UNAUTHORIZED: "pill pill-danger", PAYMENT_REQUIRED: "pill pill-danger",
  UNAVAILABLE: "pill pill-warn" };
const label = (s: string) => s.replaceAll("_", " ");

function scalar(v: any): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toLocaleString(undefined, { maximumFractionDigits: 4 });
  return String(v);
}

/** Provider labels / names for one wallet, compact (table cell). */
export function ExternalBadges({ ext }: { ext: J | undefined }) {
  const entries = Object.entries(ext ?? {}) as [string, J][];
  if (entries.length === 0) return <span className="muted small">—</span>;
  return (
    <span className="small">
      {entries.map(([p, e]) => (
        <span key={p} title={`${NAME[p] ?? p} (${e.note})${e.error ? `: ${e.error}` : ""}`} style={{ marginRight: 6 }}>
          {e.status === "OK"
            ? <>{NAME[p] ?? p}: {[e.name, ...(e.labels ?? [])].filter(Boolean).join(", ") || <span className="muted">no label</span>}</>
            : <span className={STATUS_PILL[e.status] ?? "pill pill-off"}>{NAME[p] ?? p} {label(e.status)}</span>}
        </span>
      ))}
    </span>
  );
}

/** Provider-reported detail for one wallet, with a manual refresh (counts against the daily budget). */
export function ExternalDetail({ chain, wallet, ext, onRefreshed }: { chain: string; wallet: string; ext: J | undefined; onRefreshed?: () => void }) {
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const refresh = async () => {
    setBusy(true); setMsg(null);
    try {
      const r = await apiPost<J>(`/api/wallets/enrichment/${chain}/${wallet}/refresh`);
      setMsg(Object.entries(r.results as Record<string, string>).map(([p, s]) => `${NAME[p] ?? p}: ${label(s)}`).join(" · "));
      onRefreshed?.();
    } catch (e) { setMsg(String((e as Error).message)); } finally { setBusy(false); }
  };
  const entries = Object.entries(ext ?? {}) as [string, J][];
  return (
    <div style={{ marginTop: 8 }}>
      <h4 className="small">External intelligence (provider-reported; not verified here; never a trade signal)
        <button className="btn btn-ghost btn-sm" style={{ marginLeft: 8 }} disabled={busy} onClick={refresh}>
          <RefreshCw size={12} aria-hidden /> {busy ? "Looking up..." : "Look up now"}</button></h4>
      {msg && <p className="small">{msg}</p>}
      {entries.length === 0 ? <p className="muted small">No provider record for this wallet yet.</p> : (
        <table className="data-table">
          <thead><tr><th>Provider</th><th>Status</th><th>Name / labels</th><th>Provider P/L</th><th>Fetched</th></tr></thead>
          <tbody>{entries.map(([p, e]) => (
            <tr key={p}>
              <td>{NAME[p] ?? p}</td>
              <td><span className={STATUS_PILL[e.status] ?? "pill pill-off"}>{label(e.status)}</span>{e.error && <div className="muted small">{e.error}</div>}</td>
              <td className="small">{[e.name, ...(e.labels ?? [])].filter(Boolean).join(", ") || "—"}</td>
              <td className="small">{e.pnl && Object.keys(e.pnl).length > 0
                ? <>{Object.entries(e.pnl as J).slice(0, 8).map(([k, v]) => <div key={k}>{k.replaceAll("_", " ")}: {scalar(v)}</div>)}
                  <div className="muted">{e.pnl_currency ?? "units as reported"}{e.pnl_window_days ? `, ${e.pnl_window_days} d` : ""}</div></>
                : "—"}</td>
              <td className="small">{formatDate(e.fetched_at)}</td>
            </tr>
          ))}</tbody>
        </table>
      )}
    </div>
  );
}

const FIELDS: [string, string][] = [["nansen_daily_calls", "Nansen calls per day"], ["madeonsol_daily_calls", "MadeOnSol calls per day"],
  ["refresh_hours", "Refresh each wallet every (hours)"], ["wallets_per_pass", "Wallets per pass (every 10 min)"],
  ["discovery_limit", "Candidates per provider and chain"]];

/** Settings, key status, budget use and the provider candidates. */
export function ExternalPanel({ onWatch }: { onWatch: (chain: string, wallet: string) => void }) {
  const { data, error, reload } = useApi<J>("/api/wallets/enrichment", undefined, { refreshMs: 60000 });
  const [draft, setDraft] = useState<J | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  if (error) return <Section title="External intelligence (Nansen, MadeOnSol)"><ErrorNotice error={error} /></Section>;
  if (!data) return null;
  const vals = draft ?? data.settings;
  const save = async () => {
    try {
      const body: J = { ...vals };
      for (const [k] of FIELDS) body[k] = Number(body[k]);
      await apiPut("/api/wallets/enrichment-settings", body); setMsg("Saved. Used from the next pass (every 10 minutes)."); setDraft(null); reload();
    } catch (e) { setMsg(String((e as Error).message)); }
  };
  return (
    <Section title="External intelligence (Nansen, MadeOnSol)">
      <p className="muted small">{data.note}</p>
      <div className="table-scroll">
        <table className="data-table">
          <thead><tr><th>Provider</th><th>Key</th><th>Chains</th><th>Calls today</th><th>Records</th></tr></thead>
          <tbody>{Object.entries(data.providers as Record<string, J>).map(([p, v]) => (
            <tr key={p}>
              <td>{NAME[p] ?? p}</td>
              <td>{v.configured ? <span className="pill pill-ok">configured</span>
                : <span className="pill pill-off" title={`set ${v.key} in Settings`}>not configured</span>}</td>
              <td className="small">{v.chains.join(", ")}</td>
              <td>{v.calls_today} / {v.daily_budget}</td>
              <td className="small">{Object.entries(v.records as Record<string, number>).map(([k, n]) => `${label(k)} ${n}`).join(" · ") || "—"}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      <div className="form-grid">
        <label className="small"><input type="checkbox" checked={!!vals.enabled} onChange={(e) => setDraft({ ...vals, enabled: e.target.checked })} /> Enrichment on (paid calls)</label>
        <label className="small"><input type="checkbox" checked={!!vals.discovery} onChange={(e) => setDraft({ ...vals, discovery: e.target.checked })} /> Discovery candidates (once a day)</label>
        {FIELDS.map(([k, l]) => (
          <label key={k} className="small">{l}<input value={vals[k]} inputMode="numeric" onChange={(e) => setDraft({ ...vals, [k]: e.target.value })} /></label>
        ))}
      </div>
      <div className="btn-row"><button className="btn btn-sm" disabled={!draft} onClick={save}>Save</button>
        <button className="btn btn-ghost btn-sm" onClick={() => setDraft({ ...data.defaults })}>Reset to defaults</button></div>
      {msg && <p className="small">{msg}</p>}
      <h4>Candidates suggested by the providers</h4>
      {data.candidates.length === 0 ? <Empty>No candidates (discovery off, no key, or nothing found yet).</Empty> : (
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Chain</th><th>Wallet</th><th>Provider</th><th>Name / label</th><th>Source</th><th>Own history here</th><th>Suggested</th><th /></tr></thead>
            <tbody>{(data.candidates as J[]).map((c) => (
              <tr key={`${c.chain}:${c.wallet}:${c.provider}`}>
                <td>{c.chain}</td>
                <td className="mono small" title={c.wallet}>{c.wallet.slice(0, 6)}…{c.wallet.slice(-4)}</td>
                <td>{NAME[c.provider] ?? c.provider}</td>
                <td className="small">{[c.name, ...(c.labels ?? [])].filter(Boolean).join(", ") || "—"}</td>
                <td className="small">{c.source ?? "—"}</td>
                <td className="small">{c.own_history ? `${c.own_history.trades} trades · ${label(c.own_history.stage ?? "no stage")}`
                  : <span className="muted">none yet (INSUFFICIENT DATA)</span>}</td>
                <td className="small">{formatDate(c.discovered_at)}</td>
                <td><button className="btn btn-ghost btn-sm" title="adds a NOTIFY target: alerts only, nothing is bought" onClick={() => onWatch(c.chain, c.wallet)}>Watch</button></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </Section>
  );
}
