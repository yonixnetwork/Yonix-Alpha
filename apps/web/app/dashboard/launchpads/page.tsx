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

export default function LaunchpadsPage() {
  const [chain, setChain] = useState("");
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
      <ErrorNotice error={error ?? msg} />
      {loading && !data && <Loading />}
      {data && data.launchpads.length === 0 && <Empty>No launchpads on this chain.</Empty>}
      {data?.launchpads.map((lp: J) => (
        <Section key={lp.key} title={`${lp.name} · ${lp.chain.toUpperCase()}`}>
          <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            <span className={STATUS_CLASS[lp.status] ?? "pill pill-off"}>{STATUS_LABEL[lp.status] ?? lp.status}</span>
            <span className="muted small">{lp.why}</span>
            <label className="small">Operator mode{" "}
              <select value={lp.operator_mode} disabled={!lp.active || !lp.supports_trading} onChange={(e) => setMode(lp.key, e.target.value)}>
                {["OFF", "PAPER", "LIVE"].map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            </label>
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
