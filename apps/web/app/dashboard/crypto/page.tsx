"use client";

// Crypto Trading - Strategy Lab. Regular crypto strategies on Bybit / Binance
// spot public market data with simulated (DEMO) execution. A separate engine
// from Memecoin Trading: its own switch, settings, tables and worker
// (yonixalpha_crypto, /api/crypto/*). Nothing on this page can place a real
// order: the engine has no live mode and holds no exchange key.

import { useState } from "react";
import { apiPost, apiPut, ApiError } from "@/lib/api";
import { formatDate, formatDecimal } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type Health = {
  ok: boolean;
  checked_at: string;
  error?: string;
  status?: number | null;
  symbols: Record<string, { listed: boolean; tradable?: boolean; status?: string; error?: string }>;
} | null;

type Exchange = {
  id: string; label: string; market_type: string; maker_fee_bps: string; taker_fee_bps: string;
  fee_source: string; note: string; health: Health;
};

type StrategyConfig = {
  strategy_id: string; enabled: boolean; mode: string; exchange: string; symbols: string[]; interval: string;
  params: Record<string, unknown>; risk: Record<string, string>; version: number; updated_at: string | null;
  updated_by: string | null;
};

type Strategy = {
  id: string; name: string; version: string; category: string; status: string; status_reason: string;
  provenance: Record<string, string>; exchanges: string[]; intervals: string[]; notes: string[];
  config: StrategyConfig | null; open_positions?: number;
};

type Status = {
  schema_ready: boolean; mode: string; kill_switch: boolean;
  worker: { at: string; state: string; memory?: { rss_mb: number | null; limit_mb: number | null } } | null;
  worker_running: boolean; worker_hint: string | null;
  engine: { enabled: boolean; emergency_stop: boolean; updated_at: string | null; updated_by: string | null } | null;
  exchanges: Exchange[]; strategies: Strategy[];
  accounts: { exchange: string; quote: string; starting_balance: string; cash: string }[];
};

type Row = Record<string, unknown>;

const RISK_LABELS: Record<string, string> = {
  risk_per_trade_quote: "Max loss per trade (USDT)",
  max_position_quote: "Max position (USDT)",
  max_open_positions: "Max open positions",
  max_daily_loss_quote: "Max daily loss (USDT)",
  max_total_exposure_quote: "Max total exposure (USDT)",
  max_spread_bps: "Max spread (bps)",
  slippage_bps: "Slippage allowance (bps)",
  max_book_age_seconds: "Max price age (s)",
};

function statusPill(s: string) {
  if (s === "IMPLEMENTED") return "pill pill-ok";
  if (s === "BLOCKED") return "pill pill-danger";
  return "pill pill-off";
}

function errText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  return e instanceof Error ? e.message : String(e);
}

function StrategyRow({ s, exchanges, onSaved }: { s: Strategy; exchanges: Exchange[]; onSaved: () => void }) {
  const [open, setOpen] = useState(false);
  const [risk, setRisk] = useState<Record<string, string>>(s.config?.risk ?? {});
  const [exchange, setExchange] = useState(s.config?.exchange ?? "");
  const [msg, setMsg] = useState<string | null>(null);
  const cfg = s.config;
  const canEnable = s.status === "IMPLEMENTED";

  async function save(change: Record<string, unknown>) {
    if (!cfg) return;
    setMsg(null);
    try {
      await apiPut(`/api/crypto/strategies/${s.id}`, { expected_version: cfg.version, ...change });
      setMsg("Saved.");
      onSaved();
    } catch (e) {
      setMsg(errText(e));
    }
  }

  return (
    <>
      <tr>
        <td>
          <div>{s.name}</div>
          <div className="muted small">{s.category} · v{s.version}</div>
        </td>
        <td>
          <span className={statusPill(s.status)}>{s.status.replace(/_/g, " ")}</span>
          {s.status_reason && <div className="muted small" style={{ maxWidth: 320 }}>{s.status_reason}</div>}
        </td>
        <td className="mono">{cfg?.exchange || "—"}</td>
        <td className="mono">{cfg?.symbols?.join(", ") || "—"} {cfg?.interval ? `· ${cfg.interval}` : ""}</td>
        <td>
          <span className={cfg?.enabled ? "pill pill-ok" : "pill pill-off"}>{cfg?.enabled ? "DEMO on" : "off"}</span>
        </td>
        <td>{s.open_positions ?? 0}</td>
        <td>
          <div className="btn-row">
            {cfg && (canEnable || cfg.enabled) ? (
              <button className="btn btn-sm"
                onClick={() => save(cfg.enabled ? { enabled: false, mode: "DISABLED" } : { enabled: true, mode: "DEMO" })}>
                {cfg.enabled ? "Disable" : "Enable DEMO"}
              </button>
            ) : (
              <span className="muted small" title="Only an IMPLEMENTED strategy can be enabled">not available</span>
            )}
            <button className="btn btn-sm btn-ghost" onClick={() => setOpen(!open)}>{open ? "Close" : "Settings"}</button>
          </div>
        </td>
      </tr>
      {open && cfg && (
        <tr>
          <td colSpan={7}>
            <div className="form-grid">
              {s.exchanges.length > 0 && (
                <label className="field">
                  <span>Exchange</span>
                  <select value={exchange} onChange={(e) => setExchange(e.target.value)}>
                    {s.exchanges.map((x) => (
                      <option key={x} value={x}>{exchanges.find((e) => e.id === x)?.label ?? x}</option>
                    ))}
                  </select>
                </label>
              )}
              {Object.keys(RISK_LABELS).map((k) => (
                <label key={k} className="field">
                  <span>{RISK_LABELS[k]}</span>
                  <input value={risk[k] ?? ""} onChange={(e) => setRisk({ ...risk, [k]: e.target.value })} />
                </label>
              ))}
            </div>
            <div className="btn-row" style={{ marginTop: 8 }}>
              <button className="btn btn-sm" onClick={() => save({ risk, ...(s.exchanges.length ? { exchange } : {}) })}>
                Save {s.id} only
              </button>
              {msg && <span className="muted small">{msg}</span>}
            </div>
            <div className="muted small" style={{ marginTop: 6 }}>
              Source: {s.provenance.repo ?? "—"} @ {s.provenance.commit ?? "—"} · licence: {s.provenance.licence ?? "—"}
              {s.notes.map((n) => <div key={n}>{n}</div>)}
              Settings version {cfg.version}, last saved {formatDate(cfg.updated_at)} by {cfg.updated_by ?? "—"}.
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

export default function CryptoStrategyLab() {
  const { data, error, reload } = useApi<Status>("/api/crypto/status", undefined, { refreshMs: 15000 });
  const signals = useApi<Row[]>(data?.schema_ready ? "/api/crypto/signals" : null, { limit: 30 }, { refreshMs: 30000 });
  const orders = useApi<Row[]>(data?.schema_ready ? "/api/crypto/orders" : null, { limit: 30 }, { refreshMs: 30000 });
  const positions = useApi<Row[]>(data?.schema_ready ? "/api/crypto/positions" : null, { limit: 30 }, { refreshMs: 30000 });
  const [msg, setMsg] = useState<string | null>(null);

  async function setEngine(change: { enabled?: boolean; emergency_stop?: boolean }, question: string) {
    if (!window.confirm(question)) return;
    try {
      await apiPut("/api/crypto/engine", change);
      setMsg(null);
      reload();
    } catch (e) {
      setMsg(errText(e));
    }
  }

  async function healthCheck() {
    try {
      await apiPost("/api/crypto/health-check");
      setMsg("Check requested: the worker runs it on its next loop (about 30 s), if the worker is running.");
    } catch (e) {
      setMsg(errText(e));
    }
  }

  const eng = data?.engine;
  const state = data?.kill_switch ? "KILL SWITCH (no new entries)" : data?.worker?.state ?? (data?.worker_running ? "?" : "WORKER NOT RUNNING");

  return (
    <div>
      <div className="page-header">
        <div className="page-title">Crypto Trading · Strategy Lab</div>
        <span className="pill pill-warn">DEMO only, simulated fills</span>
      </div>
      <div className="notice">
        Regular crypto strategies on public Bybit / Binance spot market data. Separate from Memecoin Trading: its own
        switch, settings and records. There is no live mode and no exchange key.
      </div>
      {error && <div className="error">{error}</div>}
      {msg && <div className="notice notice-warn">{msg}</div>}
      {data && !data.schema_ready && (
        <div className="notice notice-warn">The crypto schema is not installed yet. It is created when the API starts.</div>
      )}

      {data && (
        <div className="stat-grid" style={{ marginTop: 12 }}>
          <div className="stat"><div className="stat-label">Engine</div>
            <div className="stat-value">{eng ? (eng.enabled ? "ON" : "OFF") : "—"}</div></div>
          <div className="stat"><div className="stat-label">Emergency stop</div>
            <div className="stat-value">{eng?.emergency_stop ? "ENGAGED" : "off"}</div></div>
          <div className="stat"><div className="stat-label">Worker state</div>
            <div className="stat-value small">{state}</div></div>
          <div className="stat"><div className="stat-label">Worker memory</div>
            <div className="stat-value small">{data.worker?.memory?.rss_mb ?? "—"} MB
              {data.worker?.memory?.limit_mb ? ` of ${data.worker.memory.limit_mb} MB` : ""}</div></div>
        </div>
      )}
      {data?.worker_hint && <div className="muted small" style={{ marginTop: 6 }}>{data.worker_hint}</div>}
      {eng && (
        <div className="btn-row" style={{ marginTop: 8 }}>
          <button className="btn btn-sm" onClick={() => setEngine({ enabled: !eng.enabled },
            eng.enabled ? "Turn the Crypto Trading engine OFF? Open demo positions keep their stop/target protection."
              : "Turn the Crypto Trading engine ON? Only enabled DEMO strategies are evaluated; nothing real is traded.")}>
            {eng.enabled ? "Turn engine off" : "Turn engine on"}
          </button>
          <button className={eng.emergency_stop ? "btn btn-sm" : "btn btn-sm btn-danger"}
            onClick={() => setEngine({ emergency_stop: !eng.emergency_stop },
              eng.emergency_stop ? "Release the Crypto Trading emergency stop?"
                : "Engage the Crypto Trading emergency stop? New demo entries stop; protection of open positions continues. Memecoin is not affected.")}>
            {eng.emergency_stop ? "Release emergency stop" : "Emergency stop"}
          </button>
          <span className="muted small">Last change {formatDate(eng.updated_at)} by {eng.updated_by ?? "—"}</span>
        </div>
      )}

      <div className="section-title" style={{ marginTop: 20 }}>Exchanges (public market data)</div>
      <div className="table-scroll">
        <table className="data-table">
          <thead><tr><th>Exchange</th><th>Fees (maker / taker)</th><th>Reachable from the server</th><th>BTC / ETH / SOL</th></tr></thead>
          <tbody>
            {data?.exchanges.map((x) => (
              <tr key={x.id}>
                <td>{x.label}{x.note && <div className="muted small">{x.note}</div>}</td>
                <td>{x.maker_fee_bps} / {x.taker_fee_bps} bps<div className="muted small">{x.fee_source}</div></td>
                <td>
                  {!x.health ? <span className="pill pill-off">not checked yet</span> :
                    x.health.ok ? <span className="pill pill-ok">yes</span> :
                      <span className="pill pill-danger">no {x.health.status ?? ""}</span>}
                  {x.health && <div className="muted small">{formatDate(x.health.checked_at)}{x.health.error ? ` · ${x.health.error}` : ""}</div>}
                </td>
                <td className="small">
                  {x.health ? Object.entries(x.health.symbols).map(([sym, v]) => (
                    <div key={sym}>{sym}: {v.listed ? (v.tradable ? "trading" : v.status) : "not available"}</div>
                  )) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="btn-row" style={{ marginTop: 6 }}>
        <button className="btn btn-sm btn-ghost" onClick={healthCheck}>Check exchanges now</button>
      </div>

      <div className="section-title" style={{ marginTop: 20 }}>Strategies</div>
      <div className="table-scroll">
        <table className="data-table">
          <thead><tr><th>Strategy</th><th>Status</th><th>Exchange</th><th>Symbols</th><th>Mode</th><th>Open</th><th></th></tr></thead>
          <tbody>
            {data?.strategies.map((s) => <StrategyRow key={s.id} s={s} exchanges={data.exchanges} onSaved={reload} />)}
          </tbody>
        </table>
      </div>

      <div className="section-title" style={{ marginTop: 20 }}>Demo accounts</div>
      {data && data.accounts.length === 0 ? <div className="empty-state">No demo account yet (one is created per exchange on its first demo trade, 1000 USDT).</div> : (
        <table className="data-table"><thead><tr><th>Exchange</th><th>Start</th><th>Cash</th></tr></thead>
          <tbody>{data?.accounts.map((a) => (
            <tr key={a.exchange + a.quote}><td>{a.exchange}</td><td>{formatDecimal(a.starting_balance, 2)} {a.quote}</td>
              <td>{formatDecimal(a.cash, 2)} {a.quote}</td></tr>))}</tbody></table>
      )}

      {[["Recent signals", signals.data, ["created_at", "strategy_id", "exchange", "symbol", "action", "side", "reasons"]],
        ["Demo orders", orders.data, ["created_at", "strategy_id", "exchange", "symbol", "side", "status", "price", "qty", "fee_quote", "reason"]],
        ["Demo positions", positions.data, ["opened_at", "strategy_id", "exchange", "symbol", "status", "entry_price", "stop_price", "target_price", "last_price", "unrealized_pnl_quote", "realized_pnl_quote", "exit_reason"]],
      ].map(([title, rows, cols]) => (
        <div key={title as string}>
          <div className="section-title" style={{ marginTop: 20 }}>{title as string}</div>
          {!rows || (rows as Row[]).length === 0 ? <div className="empty-state">None yet.</div> : (
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr>{(cols as string[]).map((c) => <th key={c}>{c.replace(/_/g, " ")}</th>)}</tr></thead>
                <tbody>{(rows as Row[]).map((r, i) => (
                  <tr key={String(r.id ?? i)}>{(cols as string[]).map((c) => {
                    const v = r[c];
                    const text = Array.isArray(v) ? v.join("; ") : c.endsWith("_at") ? formatDate(v as string | null) : v == null ? "—" : String(v);
                    return <td key={c} className={c.endsWith("_at") ? "" : "mono small"}>{text}</td>;
                  })}</tr>
                ))}</tbody>
              </table>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
