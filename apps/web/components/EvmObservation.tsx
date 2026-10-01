"use client";

import { useState } from "react";
import { Empty, ErrorNotice, Loading, Section } from "@/components/ui";
import { apiPut } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;

const STATE_CLASS: Record<string, string> = {
  ENTERED: "pill pill-ok", QUALIFIED: "pill pill-ok", WAITING_FOR_ENTRY: "pill pill-warn", ENTRY_PENDING: "pill pill-warn",
  OBSERVING: "pill pill-off", ANALYZING: "pill pill-off", DISCOVERED: "pill pill-off", NO_ENTRY: "pill pill-off",
  EXPIRED: "pill pill-off", SAFETY_FAILURE: "pill pill-danger", REJECTED: "pill pill-danger",
};
const CATS = ["FRESH", "MIGRATED", "MOMENTUM"];

export function StatePill({ s }: { s: string }) {
  return <span className={STATE_CLASS[s] ?? "pill pill-off"}>{s.replaceAll("_", " ")}</span>;
}

const num = (v: any, d = 4) => (v === null || v === undefined || v === "" ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d }));
const pct = (v: any) => (v === null || v === undefined ? "—" : `${(Number(v) * 100).toFixed(1)}%`);
const price = (v: any) => (v === null || v === undefined ? "—" : Number(v).toLocaleString(undefined, { maximumSignificantDigits: 4, maximumFractionDigits: 20 }));

/** Rows of the §16 snapshot table: label, getter. */
const FIELDS: [string, (s: J) => any][] = [
  ["Price", (s) => price(s.price)], ["Market cap (BNB / ETH)", (s) => (s.market_cap ? num(s.market_cap, 3) : <span className="muted" title={s.market_cap_note}>—</span>)],
  ["Volume (interval)", (s) => num(s.volume)], ["Buy / sell volume", (s) => `${num(s.buy_volume)} / ${num(s.sell_volume)}`],
  ["Trades", (s) => s.trades], ["Buyers / sellers", (s) => `${s.buyers} / ${s.sellers}`],
  ["Effective buyers", (s) => s.effective_buyers ?? <span className="muted" title={s.effective_buyers_note}>—</span>],
  ["Holders (growth)", (s) => `${s.holders}${s.holder_growth !== null && s.holder_growth !== undefined ? ` (${s.holder_growth >= 0 ? "+" : ""}${s.holder_growth})` : ""}`],
  ["Liquidity (change)", (s) => `${num(s.liquidity)}${s.liquidity_change ? ` (${Number(s.liquidity_change) >= 0 ? "+" : ""}${num(s.liquidity_change)})` : ""}`],
  ["Curve progress", (s) => pct(s.curve_progress)], ["Creator trades", (s) => s.creator_trades],
  ["Top buyer share", (s) => pct(s.top_buyer_share)], ["Smart-money buyers", (s) => s.smart_money_buyers],
  ["Net flow", (s) => pct(s.net_flow)], ["Organic net flow", (s) => pct(s.organic_net_flow)],
  ["Coordination", (s) => s.manipulation ? `${(s.manipulation.action ?? "").replaceAll("_", " ")}` : "—"],
  ["Safety", (s) => s.safety ?? "—"], ["ML", (s) => s.ml ?? <span className="muted" title={s.ml_note}>n/a</span>],
  ["Decision", (s) => s.decision ?? "—"],
];

/** One token's observations: state history and the snapshot table. */
export function TokenObservations({ chain, token }: { chain: string; token: string }) {
  const { data, error, loading } = useApi<J>(`/api/evm/tokens/${chain}/${token}`, undefined, { refreshMs: 30000 });
  if (error) return <ErrorNotice error={error} />;
  if (loading && !data) return <Loading />;
  const list: J[] = data?.observations ?? [];
  if (list.length === 0) return <p className="muted small">No observation yet: one opens at launch, migration or a momentum signal.</p>;
  return (
    <div>
      {list.map((o) => (
        <div key={o.category} style={{ marginBottom: 16 }}>
          <p className="small"><strong>{o.category}</strong> <StatePill s={o.state} />{" "}
            <span className="muted">{o.observation_reason} · started {formatDate(o.started_at)} · deadline {formatDate(o.deadline)}
              {o.extensions ? ` (extended ${o.extensions}x)` : ""}{o.expiry_reason ? ` · ${o.expiry_reason}` : ""}</span></p>
          <p className="small">{o.reason}</p>
          <ol className="small muted">
            {(o.history ?? []).map((h: J, i: number) => <li key={i}>{formatDate(h.at)} <StatePill s={h.state} /> {h.reason}</li>)}
          </ol>
          {o.snapshot_labels.length > 0 && (
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr><th>Snapshot</th>{o.snapshot_labels.map((l: string) => <th key={l}>{l}</th>)}</tr></thead>
                <tbody>
                  {FIELDS.map(([label, get]) => (
                    <tr key={label}><td className="small">{label}</td>
                      {o.snapshot_labels.map((l: string) => <td key={l} className="mono small">{get(o.snapshots[l])}</td>)}</tr>))}
                </tbody>
              </table>
            </div>
          )}
          <p className="muted small">{o.snapshots?.[o.snapshot_labels[0]]?.basis}</p>
        </div>
      ))}
    </div>
  );
}

function ObservationSettings() {
  const { data, error, reload } = useApi<J>("/api/evm/observation-settings");
  const [draft, setDraft] = useState<J | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  if (error) return <ErrorNotice error={error} />;
  if (!data) return null;
  const v = draft ?? data.settings;
  const set = (k: string, val: any) => setDraft({ ...v, [k]: val });
  const save = async () => {
    const body = { ...v, snapshots_min: String(v.snapshots_min).split(",").map((x: string) => Number(x.trim())).filter((x: number) => !Number.isNaN(x)) };
    try { await apiPut("/api/evm/observation-settings", body); setMsg("Saved. Applies to observations opened from now on."); setDraft(null); reload(); }
    catch (e) { setMsg(String((e as Error).message)); }
  };
  return (
    <div>
      <div className="form-grid">
        <label className="small">Snapshots (minutes after start, comma-separated, first 0)
          <input style={{ display: "block" }} value={Array.isArray(v.snapshots_min) ? v.snapshots_min.join(",") : v.snapshots_min}
            onChange={(e) => set("snapshots_min", e.target.value)} /></label>
        {CATS.map((c) => (
          <label key={c} className="small">{c} window (minutes)
            <input style={{ display: "block" }} value={v.window_min[c]} inputMode="numeric" onChange={(e) => set("window_min", { ...v.window_min, [c]: e.target.value })} /></label>))}
        {[["active_min_trades", "Adaptive: trades in 5 min to keep observing"], ["extend_minutes", "Adaptive: extend by (minutes)"],
          ["max_window_minutes", "Adaptive: longest window (minutes)"], ["reject_after_safety_failures", "Reject after N consecutive safety FAILs"]].map(([k, label]) => (
          <label key={k} className="small">{label}
            <input style={{ display: "block" }} value={v[k]} inputMode="numeric" onChange={(e) => set(k, e.target.value)} /></label>))}
      </div>
      <p className="muted small">Adaptive windows apply to: {(v.adaptive_categories ?? []).join(", ")}.</p>
      <div className="btn-row"><button className="btn btn-sm" disabled={!draft} onClick={save}>Save observation settings</button>
        <button className="btn btn-ghost btn-sm" onClick={() => setDraft({ ...data.defaults })}>Reset to defaults</button></div>
      {msg && <p className="small">{msg}</p>}
    </div>
  );
}

/** Observation overview for one chain: counts, what held expired tokens, recent observations. */
export function ObservationPanel({ chain, onSelect }: { chain: string; onSelect?: (token: string) => void }) {
  const [state, setState] = useState("");
  const [open, setOpen] = useState(false);
  const { data, error, loading } = useApi<J>("/api/evm/observations", { chain, hours: 24, limit: 50, ...(state ? { state } : {}) }, { refreshMs: 20000 });
  return (
    <Section title="Observation (last 24 h)">
      <p className="muted small">{data?.note}. A token can only be entered while its observation is open; one without a qualifying entry by its deadline ends EXPIRED NO ENTRY.</p>
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {data && (
        <>
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>Category</th>{data.states.map((s: string) => <th key={s} className="small">{s.replaceAll("_", " ")}</th>)}</tr></thead>
              <tbody>
                {CATS.map((c) => (
                  <tr key={c}><td>{c}</td>{data.states.map((s: string) => <td key={s}>{data.counts?.[c]?.[s] ?? 0}</td>)}</tr>))}
              </tbody>
            </table>
          </div>
          {Object.keys(data.expired_held_by).length > 0 && (
            <p className="small">Expired tokens were held by: {Object.entries(data.expired_held_by).map(([k, n]) => `${k.replaceAll("_", " ")} ${n}`).join(" · ")}</p>)}
          <div className="btn-row">
            {["", "OBSERVING", "ANALYZING", "QUALIFIED", "WAITING_FOR_ENTRY", "ENTERED", "EXPIRED", "REJECTED"].map((s) => (
              <button key={s || "all"} aria-pressed={state === s} className={state === s ? "btn btn-sm" : "btn btn-ghost btn-sm"} onClick={() => setState(s)}>
                {s ? s.replaceAll("_", " ") : "All"}</button>))}
          </div>
          {data.observations.length === 0 ? <Empty>No observation in this view yet.</Empty> : (
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr><th>Token</th><th>Launchpad</th><th>Category</th><th>State</th><th>Started</th><th>Deadline</th><th>Snapshots</th><th>Last: buyers / net flow</th><th>Reason</th></tr></thead>
                <tbody>
                  {data.observations.map((o: J) => (
                    <tr key={`${o.token}-${o.category}`} style={{ cursor: onSelect ? "pointer" : undefined }} onClick={() => onSelect?.(o.token)}>
                      <td className="mono small" title={o.token}>{o.symbol ?? o.token.slice(0, 10)}</td>
                      <td>{o.launchpad}</td><td>{o.category}</td><td><StatePill s={o.state} /></td>
                      <td className="small">{formatDate(o.started_at)}</td><td className="small">{formatDate(o.deadline)}</td>
                      <td className="small">{o.snapshot_labels.join(" ") || "—"}</td>
                      <td className="small">{o.last_snapshot ? `${o.last_snapshot.buyers} / ${pct(o.last_snapshot.net_flow)}` : "—"}</td>
                      <td className="small">{o.expiry_reason && !String(o.reason ?? "").startsWith(o.expiry_reason) ? `${o.expiry_reason}: ` : ""}{o.reason}</td>
                    </tr>))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
      <button className="btn btn-ghost btn-sm" onClick={() => setOpen(!open)}>{open ? "Hide settings" : "Settings"}</button>
      {open && <ObservationSettings />}
    </Section>
  );
}
