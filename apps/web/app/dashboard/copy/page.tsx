"use client";

import { useState } from "react";
import { Copy, Plus, Power, Timer, Trash2 } from "lucide-react";
import { Empty, ErrorNotice, Loading, Money, PageHeader, Section } from "@/components/ui";
import { apiDelete, apiPatch, apiPost } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const DECISION_CLASS: Record<string, string> = {
  COPIED: "pill pill-ok", NOTIFIED: "pill pill-warn", SKIPPED: "pill pill-off", FAILED: "pill pill-danger", PENDING: "pill pill-off",
};
const short = (a: string) => (a && a.length > 14 ? `${a.slice(0, 6)}…${a.slice(-4)}` : a);

function AddTarget({ onDone }: { onDone: () => void }) {
  const [f, setF] = useState<J>({ chain: "bsc", wallet: "", label: "", mode: "NOTIFY", fixed_size: "", chase_guard_pct: "0.15", max_delay_seconds: "30" });
  const [msg, setMsg] = useState<string | null>(null);
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const settings: J = { chase_guard_pct: f.chase_guard_pct, max_delay_seconds: Number(f.max_delay_seconds) };
    if (f.fixed_size) settings.fixed_size = f.fixed_size;
    try {
      await apiPost("/api/copy/targets", { chain: f.chain, wallet: f.wallet.trim(), label: f.label || null, mode: f.mode, settings });
      setMsg(null); setF({ ...f, wallet: "", label: "" }); onDone();
    } catch (err) { setMsg(String((err as Error).message)); }
  };
  return (
    <form onSubmit={submit} className="form-grid" style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "end" }}>
      <label className="small">Chain<br />
        <select value={f.chain} onChange={set("chain")}><option value="solana">Solana</option><option value="bsc">BSC</option><option value="robinhood">Robinhood Chain</option></select>
      </label>
      <label className="small" style={{ flex: "1 1 320px" }}>Wallet<br /><input value={f.wallet} onChange={set("wallet")} required placeholder="address" style={{ width: "100%" }} /></label>
      <label className="small">Label<br /><input value={f.label} onChange={set("label")} maxLength={64} /></label>
      <label className="small">Mode<br />
        <select value={f.mode} onChange={set("mode")}><option value="NOTIFY">NOTIFY (signal only)</option><option value="BUY_ONLY">BUY ONLY</option><option value="MIRROR">MIRROR (buys and sells)</option></select>
      </label>
      <label className="small">Size (native, blank = default)<br /><input value={f.fixed_size} onChange={set("fixed_size")} inputMode="decimal" size={8} /></label>
      <label className="small">Chase guard<br /><input value={f.chase_guard_pct} onChange={set("chase_guard_pct")} inputMode="decimal" size={5} /></label>
      <label className="small">Max delay (s)<br /><input value={f.max_delay_seconds} onChange={set("max_delay_seconds")} inputMode="numeric" size={4} /></label>
      <button className="btn btn-sm" type="submit"><Plus size={14} aria-hidden /> Add target</button>
      <ErrorNotice error={msg} />
    </form>
  );
}

function Targets() {
  const { data, error, loading, reload } = useApi<J>("/api/copy/targets", undefined, { refreshMs: 15000, reloadOn: ["copy.event", "copy.targets.updated"] });
  const [msg, setMsg] = useState<string | null>(null);
  const act = async (fn: () => Promise<unknown>) => { try { await fn(); setMsg(null); reload(); } catch (e) { setMsg(String((e as Error).message)); } };
  return (
    <Section title="Copy targets">
      <p className="muted small">{data?.note ?? ""}</p>
      <AddTarget onDone={reload} />
      <ErrorNotice error={error ?? msg} />
      {loading && !data && <Loading />}
      {data && data.targets.length === 0 && <Empty>No copy targets. Add a wallet above (start with NOTIFY to watch before copying).</Empty>}
      {data && data.targets.length > 0 && (
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Chain</th><th>Wallet</th><th>Label</th><th>Mode</th><th>Size</th><th>Events</th><th>Open</th><th>Status</th><th /></tr></thead>
            <tbody>
              {data.targets.map((t: J) => (
                <tr key={t.id}>
                  <td>{t.chain}</td>
                  <td className="mono small" title={t.wallet}>{short(t.wallet)}</td>
                  <td>{t.label ?? "—"}</td>
                  <td>
                    <select value={t.mode} onChange={(e) => act(() => apiPatch(`/api/copy/targets/${t.id}`, { mode: e.target.value }))} aria-label="mode">
                      {data.modes.map((m: string) => <option key={m} value={m}>{m}</option>)}
                    </select>
                  </td>
                  <td className="small">{t.settings.size_mode === "FIXED" ? (t.settings.fixed_size ?? "default") : `${Number(t.settings.proportional_pct) * 100}% of target`}</td>
                  <td className="small">{Object.entries(t.stats?.events ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "—"}</td>
                  <td>{t.stats?.open_positions ?? 0}</td>
                  <td>
                    <button className="btn btn-ghost btn-sm" onClick={() => act(() => apiPatch(`/api/copy/targets/${t.id}`, { enabled: !t.enabled }))}>
                      <Power size={14} aria-hidden /> {t.enabled ? "ON" : "OFF"}
                    </button>
                  </td>
                  <td>
                    <button className="btn btn-ghost btn-sm" aria-label="remove target"
                      onClick={() => window.confirm(`Remove ${t.wallet}? Open copy positions keep their stops.`) && act(() => apiDelete(`/api/copy/targets/${t.id}`))}>
                      <Trash2 size={14} aria-hidden />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

function Positions() {
  const { data, error } = useApi<J>("/api/copy/positions", undefined, { refreshMs: 10000, reloadOn: ["copy.event", "position.updated"] });
  return (
    <Section title="Open copy positions (paper)">
      <ErrorNotice error={error} />
      {data && data.positions.length === 0 && <Empty>No open copy positions.</Empty>}
      {data && data.positions.length > 0 && (
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Chain</th><th>Token</th><th>Copy of</th><th>Mode</th><th>Opened</th><th>Cost</th><th>Unrealized</th></tr></thead>
            <tbody>
              {data.positions.map((p: J) => (
                <tr key={p.id}>
                  <td>{p.chain}</td><td className="mono small">{p.symbol}</td><td className="mono small">{p.target_label ?? short(p.target)}</td>
                  <td>{p.mode}</td><td>{formatDate(p.entry_at)}</td>
                  <td>{Number(p.entry_cost).toFixed(4)} <span className="unit">{p.currency}</span></td>
                  <td><Money value={p.unrealized_pnl} currency={p.currency} digits={6} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

function Events() {
  const { data, error } = useApi<J>("/api/copy/events", { limit: 100 }, { refreshMs: 10000, reloadOn: ["copy.event"] });
  const med = data?.median_latency_ms_copied ?? {};
  return (
    <Section title="Copy events">
      <ErrorNotice error={error} />
      {data && (
        <p className="muted small"><Timer size={14} aria-hidden /> Median latency of copied trades (ms): detection {med.detection ?? "—"} · analysis {med.analysis ?? "—"} ·
          risk {med.risk ?? "—"} · execution {med.execution ?? "—"} · total {med.total ?? "—"}. Detection includes the delay of the chain feed itself (EVM: block confirmations).</p>
      )}
      {data && data.events.length === 0 && <Empty>No target trades observed yet.</Empty>}
      {data && data.events.length > 0 && (
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Seen</th><th>Chain</th><th>Target</th><th>Side</th><th>Token</th><th>Target spent</th><th>Decision</th><th>Reason</th><th>Total ms</th></tr></thead>
            <tbody>
              {data.events.map((e: J) => (
                <tr key={e.id}>
                  <td>{formatDate(e.detected_at)}</td><td>{e.chain}</td><td className="mono small">{short(e.wallet)}</td>
                  <td className={e.side === "BUY" ? "pos" : "neg"}>{e.side}</td>
                  <td className="mono small" title={e.token}>{short(e.token)}</td>
                  <td>{Number(e.target_quote_amount).toFixed(4)}</td>
                  <td><span className={DECISION_CLASS[e.decision] ?? "pill pill-off"}>{e.decision}</span></td>
                  <td className="small">{e.reason ?? "—"}</td>
                  <td>{e.latency_ms?.total ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

export default function CopyTradingPage() {
  return (
    <div>
      <PageHeader title="Copy Trading" icon={<Copy size={20} aria-hidden />}
        subtitle="Paper only. A target's trade is a candidate: every copied buy still passes the kill switch, trading controls, the Solana gate or the EVM launchpad evidence and safety checks, the chase guard and the risk plan." />
      <Targets />
      <Positions />
      <Events />
    </div>
  );
}
