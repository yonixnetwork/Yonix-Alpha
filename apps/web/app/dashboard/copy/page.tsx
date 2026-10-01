"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight, Copy, Plus, Power, Timer, Trash2 } from "lucide-react";
import { Empty, ErrorNotice, Loading, Money, PageHeader, Section } from "@/components/ui";
import { apiDelete, apiPatch, apiPost } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const DECISION_CLASS: Record<string, string> = {
  COPIED: "pill pill-ok", NOTIFIED: "pill pill-warn", SKIPPED: "pill pill-off", FAILED: "pill pill-danger", PENDING: "pill pill-off",
};
const short = (a: string) => (a && a.length > 14 ? `${a.slice(0, 6)}…${a.slice(-4)}` : a);
const px = (v: any) => (v === null || v === undefined ? "—" : Number(v).toPrecision(4));
const qty = (v: any) => (v === null || v === undefined ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
const pctv = (v: any, d = 1) => (v === null || v === undefined ? "—" : `${Number(v) > 0 ? "+" : ""}${Number(v).toFixed(d)}%`);
const sign = (v: any) => (v === null || v === undefined ? "" : Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : "");
const paidMore = (v: any) => (v === null || v === undefined ? "" : Number(v) > 0 ? "neg" : Number(v) < 0 ? "pos" : "");  // displacement: higher entry is worse
const STAGES = ["detection", "analysis", "risk", "decision", "execution", "build", "sign", "submission", "landing", "confirmation", "total"];
const CLASS_LABEL: Record<string, string> = {
  COPIED: "Copied", MISSED: "Missed (late, limits, cash, switches)", BLOCKED_BY_SAFETY: "Blocked by safety / risk",
  FILTERED_BY_SETTINGS: "Filtered by target settings", NOT_COPYABLE: "Not copyable (token / venue)", NOTIFY_ONLY: "Notify only",
};

function OutcomeCell({ o }: { o: J | null }) {
  if (!o) return <span className="muted small">pending</span>;
  if (o.status !== "EVALUATED") return <span className="muted small" title={o.reason}>no price data</span>;
  return <span className={o.label === "WOULD_HAVE_WON" ? "pos small" : "neg small"} title={`${o.basis ?? ""}; exit by ${o.exit_by}`}>
    {o.label === "WOULD_HAVE_WON" ? "would have won" : "would have lost"} {pctv(o.result_pct)}</span>;
}

function LinkDetail({ l, currency }: { l: J; currency: string }) {
  const lat = l.copy_latency ?? {};
  return (
    <div className="small" style={{ padding: "6px 0" }}>
      <div className="stat-grid">
        <div className="stat"><div className="stat-label">Source wallet / transaction</div>
          <div className="stat-value small mono" title={l.source_transaction ?? l.source_event_id}>{short(l.source_wallet)} / {l.source_transaction ? short(l.source_transaction) : "not available (stream has no signature)"}</div></div>
        <div className="stat"><div className="stat-label">Target position (tokens)</div>
          <div className="stat-value small">bought {qty(l.source_position?.bought)} · still held {qty(l.source_position?.still_held)}</div></div>
        <div className="stat"><div className="stat-label">Copy ratio / mode</div><div className="stat-value small">{px(l.copy_ratio)} / {l.copy_mode}</div></div>
        <div className="stat"><div className="stat-label">Entry: target / ours ({currency} per token)</div>
          <div className="stat-value small">{px(l.target_entry)} / {px(l.our_entry)} <span className={paidMore(l.price_displacement_pct)}>({pctv(l.price_displacement_pct, 2)})</span></div></div>
        <div className="stat"><div className="stat-label">Exit: target / ours</div>
          <div className="stat-value small">{px(l.target_exit)}{l.target_sold_fraction ? ` (${(Number(l.target_sold_fraction) * 100).toFixed(0)}% sold)` : ""} / {px(l.our_exit)}</div></div>
        <div className="stat"><div className="stat-label">Slippage</div><div className="stat-value small" title={l.slippage_note}>{l.slippage ?? "paper: n/a"}</div></div>
        <div className="stat"><div className="stat-label">PnL ({l.pnl_kind})</div><div className="stat-value small"><Money value={l.pnl} currency={currency} digits={6} /></div></div>
        <div className="stat"><div className="stat-label">Copy latency (ms)</div>
          <div className="stat-value small">{STAGES.filter((k) => k in lat).map((k) => `${k} ${lat[k] ?? "live only"}`).join(" · ") || "—"}</div></div>
      </div>
    </div>
  );
}

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
        <select value={f.mode} onChange={set("mode")}><option value="NOTIFY">NOTIFY (signal only)</option><option value="BUY_ONLY">BUY ONLY</option><option value="MIRROR">MIRROR (buys and sells)</option><option value="SELL_ONLY">SELL ONLY (exits of our own positions)</option></select>
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
  const [status, setStatus] = useState("open");
  const [open, setOpen] = useState<string | null>(null);
  const { data, error } = useApi<J>("/api/copy/positions", { status }, { refreshMs: 10000, reloadOn: ["copy.event", "position.updated"] });
  return (
    <Section title="Copy positions (paper)">
      <div style={{ display: "flex", gap: 8, margin: "0 0 8px" }}>
        {["open", "closed"].map((v) => <button key={v} className={status === v ? "btn btn-sm" : "btn btn-ghost btn-sm"} onClick={() => setStatus(v)}>{v === "open" ? "Open" : "Closed"}</button>)}
      </div>
      <ErrorNotice error={error} />
      {data && data.positions.length === 0 && <Empty>No {status} copy positions.</Empty>}
      {data && data.positions.length > 0 && (
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th /><th>Chain</th><th>Token</th><th>Copy of</th><th>Mode</th><th>Opened</th><th>Target entry</th><th>Our entry</th><th>Displacement</th><th>Target exit</th><th>Our exit</th><th>Latency</th><th>PnL</th></tr></thead>
            <tbody>
              {data.positions.map((p: J) => {
                const l = p.link ?? {};
                return [
                  <tr key={p.id}>
                    <td><button className="btn btn-ghost btn-sm" aria-expanded={open === p.id} aria-label="copy link detail" onClick={() => setOpen(open === p.id ? null : p.id)}>
                      {open === p.id ? <ChevronDown size={14} aria-hidden /> : <ChevronRight size={14} aria-hidden />}</button></td>
                    <td>{p.chain}</td><td className="mono small">{p.symbol}</td><td className="mono small">{p.target_label ?? short(p.target)}</td>
                    <td>{p.mode}</td><td>{formatDate(p.entry_at)}</td>
                    <td>{px(l.target_entry)}</td><td>{px(l.our_entry)}</td>
                    <td className={paidMore(l.price_displacement_pct)}>{pctv(l.price_displacement_pct, 2)}</td>
                    <td>{px(l.target_exit)}</td><td>{px(l.our_exit)}</td>
                    <td>{l.copy_latency?.total ?? "—"}{l.copy_latency?.total !== undefined ? " ms" : ""}</td>
                    <td><Money value={l.pnl ?? p.realized_pnl} currency={p.currency} digits={6} /></td>
                  </tr>,
                  open === p.id && <tr key={`${p.id}:link`}><td colSpan={13}><LinkDetail l={l} currency={p.currency} /></td></tr>,
                ];
              })}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted small">Prices are in the chain currency (SOL / BNB / ETH) per whole token. Displacement: our entry against the target&apos;s (positive = we paid more). Slippage is measured on live fills only; a paper fill is the executable quote at decision time.</p>
    </Section>
  );
}

function Outcomes() {
  const { data, error } = useApi<J>("/api/copy/outcomes", undefined, { refreshMs: 60000, reloadOn: ["copy.event"] });
  return (
    <Section title="Paper copy outcomes">
      <p className="muted small">{data ? `${data.note}. Each target buy is evaluated ${data.horizon_min} minutes after we saw it: entry at the first trade after that, exit at the target's own sell (MIRROR targets) or at the horizon. Basis: ${data.basis}.` : ""}</p>
      <ErrorNotice error={error} />
      {data && data.summary.length === 0 && <Empty>No evaluated target buys yet (each needs {data.horizon_min} minutes after it was seen).</Empty>}
      {data && data.summary.length > 0 && (
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Target</th><th>Chain</th><th>Mode</th><th>What we did</th><th>Buys</th><th>Evaluated</th><th>Would have won / lost</th><th>Won rate</th><th>Avg result</th><th>Median result</th><th>No price data</th></tr></thead>
            <tbody>
              {data.summary.map((g: J) => (
                <tr key={`${g.target_id}:${g.class}`}>
                  <td className="mono small" title={g.wallet}>{g.label ?? short(g.wallet)}</td><td>{g.chain}</td><td>{g.mode}</td>
                  <td className="small">{CLASS_LABEL[g.class] ?? g.class}</td>
                  <td>{g.events}</td><td>{g.evaluated}</td>
                  <td><span className="pos">{g.won}</span> / <span className="neg">{g.lost}</span></td>
                  <td>{g.won_rate === null ? <span className="muted small">insufficient data</span> : `${(g.won_rate * 100).toFixed(0)}%`}</td>
                  <td className={sign(g.avg_result_pct)}>{pctv(g.avg_result_pct)}</td>
                  <td className={sign(g.median_result_pct)}>{pctv(g.median_result_pct)}</td>
                  <td>{g.no_price_data}</td>
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
          risk {med.risk ?? "—"} · decision {med.decision ?? "—"} · execution {med.execution ?? "—"} · total {med.total ?? "—"}.
          {" "}{(data.live_only_stages ?? []).join(" / ")}: live only (copy trading is paper). Detection includes the delay of the chain feed itself (EVM: block confirmations).</p>
      )}
      {data && data.events.length === 0 && <Empty>No target trades observed yet.</Empty>}
      {data && data.events.length > 0 && (
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Seen</th><th>Chain</th><th>Target</th><th>Side</th><th>Token</th><th>Target spent</th><th>Decision</th><th>Reason</th><th>Total ms</th><th>Outcome (paper)</th></tr></thead>
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
                  <td>{e.side === "BUY" ? <OutcomeCell o={e.outcome} /> : <span className="muted small">—</span>}</td>
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
      <Outcomes />
      <Events />
    </div>
  );
}
