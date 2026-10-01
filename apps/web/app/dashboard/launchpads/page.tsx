"use client";

import { useState } from "react";
import { CheckCircle2, CircleDashed, Layers, OctagonX, Power, ShieldCheck, XCircle } from "lucide-react";
import { Empty, ErrorNotice, Loading, PageHeader, Section } from "@/components/ui";
import { apiPost, apiPut } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const CHAIN_TABS: [string, string][] = [["", "All chains"], ["solana", "Solana"], ["bsc", "BSC"], ["robinhood", "Robinhood Chain"]];
const STATUS_CLASS: Record<string, string> = {
  LIVE: "pill pill-ok", PAPER_ONLY: "pill pill-warn", DEGRADED: "pill pill-danger", UNVERIFIED: "pill pill-off", DISABLED: "pill pill-off",
};
const STATUS_LABEL: Record<string, string> = { PAPER_ONLY: "PAPER", LIVE: "LIVE", DEGRADED: "DEGRADED", UNVERIFIED: "UNVERIFIED", DISABLED: "DISABLED" };
const ACTIVITY_CLASS: Record<string, string> = {
  ACTIVE: "pill pill-ok", QUIET: "pill pill-warn", DEGRADED: "pill pill-danger", UNVERIFIED: "pill pill-off", INACTIVE: "pill pill-off", DISABLED: "pill pill-off",
};
const LIST_TABS: [string, string][] = [["listed", "Active"], ["archived", "Archived / inactive adapters"], ["all", "All"]];

function n(v: number | null | undefined) { return v === null || v === undefined ? "not tracked" : v.toLocaleString(); }

const SWITCH_LABEL: Record<string, string> = {
  new_entries: "New entries", sniper: "Sniper", copy: "Copy trading", "chain:solana": "Solana", "chain:bsc": "BSC", "chain:robinhood": "Robinhood Chain",
};

function CheckIcon({ status }: { status: string }) {
  if (status === "PASS") return <CheckCircle2 size={14} aria-label="passed" className="pos" />;
  if (status === "FAIL") return <XCircle size={14} aria-label="failed" className="neg" />;
  return <CircleDashed size={14} aria-label="not run" className="muted" />;
}

function Controls() {
  const { data, error, reload } = useApi<J>("/api/controls", undefined, { reloadOn: ["controls.updated", "kill_switch.updated"] });
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const toggle = async (key: string, enabled: boolean) => {
    setBusy(key);
    try { await apiPut(`/api/controls/${key}`, { enabled }); setMsg(null); reload(); } catch (e) { setMsg(String((e as Error).message)); }
    setBusy(null);
  };
  const act = async (path: string, phrase: string) => {
    const typed = window.prompt(`Type ${phrase} to confirm`);
    if (typed !== phrase) return;
    try { const r = await apiPost<J>(path, { confirm: phrase }); setMsg(JSON.stringify(r)); reload(); } catch (e) { setMsg(String((e as Error).message)); }
  };
  return (
    <Section title="Trading controls">
      <ErrorNotice error={error ?? msg} />
      {data && (
        <>
          <p className="muted small">{data.note}. Global kill switch: <span className={data.kill_switch.engaged ? "pill pill-danger" : "pill pill-ok"}>
            {data.kill_switch.engaged ? `ENGAGED — ${data.kill_switch.reason ?? ""}` : "off"}</span></p>
          <div className="stat-grid">
            {Object.entries(data.switches as Record<string, J>).map(([k, v]) => (
              <div key={k} className="stat">
                <div className="stat-label">{SWITCH_LABEL[k] ?? k}</div>
                <div className="stat-value">
                  <span className={v.enabled ? "pill pill-ok" : "pill pill-danger"}>{v.enabled ? "ON" : "OFF"}</span>{" "}
                  <button className="btn btn-ghost btn-sm" disabled={busy === k} onClick={() => toggle(k, !v.enabled)}>
                    <Power size={14} aria-hidden /> Turn {v.enabled ? "off" : "on"}
                  </button>
                </div>
                {v.updated_by && <div className="form-hint">by {v.updated_by} · {formatDate(v.updated_at)}</div>}
              </div>
            ))}
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
            <button className="btn btn-danger" onClick={() => act("/api/controls/close-positions", "CLOSE POSITIONS")}>
              <OctagonX size={16} aria-hidden /> Close positions
            </button>
            <button className="btn btn-danger" onClick={() => act("/api/controls/emergency-exit", "EMERGENCY EXIT")}>
              <OctagonX size={16} aria-hidden /> Emergency exit
            </button>
          </div>
        </>
      )}
    </Section>
  );
}

function shown(lps: J[], list: string): J[] {
  if (list === "all") return lps;
  return lps.filter((lp) => (list === "listed" ? lp.listed : !lp.listed));
}

export default function LaunchpadsPage() {
  const [chain, setChain] = useState("");
  const [list, setList] = useState("listed");
  const { data, error, loading, reload } = useApi<J>("/api/launchpads", chain ? { chain } : undefined, { refreshMs: 60000, reloadOn: ["controls.updated"] });
  const [msg, setMsg] = useState<string | null>(null);
  const setMode = async (key: string, mode: string) => {
    try { await apiPut(`/api/controls/launchpad:${key}`, { mode }); setMsg(null); reload(); } catch (e) { setMsg(String((e as Error).message)); }
  };
  return (
    <div>
      <PageHeader title="Launchpads" icon={<Layers size={20} aria-hidden />}
        subtitle="Status is computed from evidence recorded on the real chain. A launchpad trades live only after a verified buy and sell." />
      <Controls />
      <div className="tabs" role="tablist" style={{ display: "flex", gap: 8, margin: "12px 0" }}>
        {CHAIN_TABS.map(([v, label]) => (
          <button key={v} role="tab" aria-selected={chain === v} className={chain === v ? "btn btn-sm" : "btn btn-ghost btn-sm"} onClick={() => setChain(v)}>{label}</button>
        ))}
      </div>
      <div className="tabs" role="tablist" style={{ display: "flex", gap: 8, margin: "0 0 12px" }}>
        {LIST_TABS.map(([v, label]) => (
          <button key={v} role="tab" aria-selected={list === v} className={list === v ? "btn btn-sm" : "btn btn-ghost btn-sm"} onClick={() => setList(v)}>{label}</button>
        ))}
      </div>
      <p className="muted small">Active lists launchpads with a launch, trade or migration recorded in the last 7 days. After 7 days without any
        (and at least 7 days of monitoring) a launchpad moves to archived / inactive adapters; its adapter keeps scanning and it returns here when activity resumes.</p>
      <ErrorNotice error={error ?? msg} />
      {loading && !data && <Loading />}
      {data && shown(data.launchpads, list).length === 0 && <Empty>No launchpads in this list.</Empty>}
      {data && shown(data.launchpads, list).map((lp: J) => (
        <Section key={lp.key} title={`${lp.name} · ${lp.chain.toUpperCase()}`}>
          <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            <span className={ACTIVITY_CLASS[lp.activity_status] ?? "pill pill-off"} title="activity">{lp.activity_status}</span>
            <span className="muted small">{lp.activity_why}</span>
            <span className={STATUS_CLASS[lp.status] ?? "pill pill-off"} title="verification">{STATUS_LABEL[lp.status] ?? lp.status}</span>
            <span className="muted small">{lp.why}</span>
            {lp.supports_trading ? (
              <label className="small">Operator mode{" "}
                <select value={lp.operator_mode} disabled={!lp.active} onChange={(e) => setMode(lp.key, e.target.value)}>
                  {["OFF", "PAPER", "LIVE"].map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
              </label>) : <span className="pill pill-off" title="activity is monitored; nothing is traded or copied here">OBSERVE ONLY</span>}
          </div>
          <div className="table-scroll">
            <table className="data-table">
              <tbody>
                <tr><th>Lifecycle</th><td>{lp.lifecycle}</td><th>Quote asset</th><td>{lp.quote_asset}</td></tr>
                <tr><th>Curve</th><td>{lp.curve_model}</td><th>Liquidity</th><td>{lp.liquidity_model}</td></tr>
                <tr><th>Migration</th><td>{lp.migration_model}</td><th>Execution</th><td>{lp.execution_model}</td></tr>
                <tr><th>Safety</th><td>{lp.safety_model}</td><th>Events</th><td className="mono small">{lp.supported_events.join(", ")}</td></tr>
              </tbody>
            </table>
          </div>
          <div className="stat-grid">
            <div className="stat"><div className="stat-label">Last launch{lp.probe ? " (seen in samples)" : ""}</div><div className="stat-value small">{lp.last_launch ? formatDate(lp.last_launch) : lp.probe ? "not seen in samples" : "none recorded"}</div></div>
            <div className="stat"><div className="stat-label">Last trade{lp.probe ? " (seen in samples)" : ""}</div><div className="stat-value small">{lp.last_trade ? formatDate(lp.last_trade) : lp.probe ? "not seen in samples" : lp.trades_7d === null ? "not tracked" : "none recorded"}</div></div>
            <div className="stat"><div className="stat-label">Last migration{lp.probe ? " (seen in samples)" : ""}</div><div className="stat-value small">{lp.last_migration ? formatDate(lp.last_migration) : lp.probe ? "not seen in samples" : "none recorded"}</div></div>
            <div className="stat"><div className="stat-label">Launches 7d</div><div className="stat-value small">{n(lp.launches_7d)}</div></div>
            <div className="stat"><div className="stat-label">Trades 7d</div><div className="stat-value small">{n(lp.trades_7d)}</div></div>
            <div className="stat"><div className="stat-label">Migrations 7d</div><div className="stat-value small">{n(lp.migrations_7d)}</div></div>
            <div className="stat"><div className="stat-label">Volume 7d ({lp.quote_asset || "quote"})</div><div className="stat-value small">{lp.volume_7d === null ? "not tracked" : Number(lp.volume_7d).toLocaleString(undefined, { maximumFractionDigits: 2 })}</div></div>
            <div className="stat"><div className="stat-label">Event monitor / buy / sell verified</div>
              <div className="stat-value small">{[lp.event_monitor_verified, lp.buy_verified, lp.sell_verified].map((v: boolean) => (v ? "yes" : "no")).join(" / ")}</div></div>
          </div>
          {lp.probe && (
            <div className="stat-grid">
              <div className="stat"><div className="stat-label">Last transaction (any instruction)</div>
                <div className="stat-value small">{lp.last_transaction ? formatDate(lp.last_transaction) : "none seen"}</div></div>
              <div className="stat"><div className="stat-label">Transactions / min (newest 1,000)</div>
                <div className="stat-value small">{lp.probe.rate_per_min ?? "—"}</div></div>
              <div className="stat"><div className="stat-label">Latest sample ({lp.probe.sampled ?? 0} tx)</div>
                <div className="stat-value small">{Object.entries(lp.probe.sample_kinds ?? {}).map(([k, v]) => `${k} ${v}`).join(", ") || "none classified"}</div></div>
              <div className="stat" title="instruction names the probe does not classify (fee claims, config...)">
                <div className="stat-label">Other instructions</div>
                <div className="stat-value small mono">{Object.entries(lp.probe.unknown_instructions ?? {}).map(([k, v]) => `${k} ${v}`).join(", ") || "—"}</div></div>
              {lp.probe.error && <div className="stat"><div className="stat-label">Probe error</div><div className="stat-value small neg">{lp.probe.error}</div></div>}
            </div>)}
          {lp.probe && <p className="muted small">Activity probe (observe only): the newest transactions of the program and a
            sample of 25 classified every 5 minutes. 7-day counts are not measured; launches can be missed by a sample.</p>}
          <div className="stat-grid">
            {Object.entries(lp.checks as Record<string, J>).map(([c, v]) => (
              <div key={c} className="stat" title={v.evidence ? JSON.stringify(v.evidence) : "never run"}>
                <div className="stat-label">{c.replace("_", " ")}</div>
                <div className="stat-value small"><CheckIcon status={v.status} /> {v.status === "NOT_RUN" ? "not run" : v.status}</div>
                {v.at && <div className="form-hint">{formatDate(v.at)}</div>}
              </div>
            ))}
          </div>
          <p className="muted small"><ShieldCheck size={14} aria-hidden /> Contracts:{" "}
            {Object.entries(lp.contracts as Record<string, string>).map(([k, a]) => `${k} ${a}`).join(" · ") || "—"}</p>
          <p className="muted small">Sources: {lp.sources.join("; ")}{lp.notes ? ` · ${lp.notes}` : ""}</p>
        </Section>
      ))}
    </div>
  );
}
