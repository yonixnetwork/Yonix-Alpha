"use client";

// Crypto Trading - Exchange Connections. API keys for Bybit / Binance demo,
// testnet and live accounts. Keys are sent once, stored encrypted by the
// backend and never shown again (only the last 4 characters). A test is
// read-only. Saving or enabling a LIVE connection authorizes nothing: the
// backend refuses every real order while the code lock is on and while any
// gate listed here fails.

import { useState } from "react";
import { apiPost, apiPut, ApiError } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type Gate = { id: string; ok: boolean; detail: string };
type Perms = {
  verified: boolean; read_only: boolean | null; withdraw_enabled: boolean | null; ip_restricted: boolean | null;
  can_trade_spot: boolean | null; can_trade_derivatives: boolean | null; notes: string[];
  balances?: { asset: string; total: string }[];
};
type Conn = {
  id: string; exchange: string; environment: string; label: string; host: string; environment_label: string;
  practice: boolean; key_hint: string; enabled: boolean; status: string; permissions: Perms | null;
  checked_at: string | null; last_ok_at: string | null; last_error: string | null; updated_by: string | null;
  live_gates: Gate[] | null; checks: Checks | null;
};
type Check = { state: string; ok: boolean; message: string; http_status?: number | null; code?: number | null };
type Checks = { public_market_data: Check; account_auth: Check; order_validation: Check; live_orders: Check;
  host: string; environment: string; origin: string };

// The four checks of a connection test, each with its own result: a public
// endpoint answering never proves the key, and a 451 location block is never
// shown as an authentication failure.
const CHECK_LABEL: [keyof Checks, string][] = [["public_market_data", "Public market data (this host, no key)"],
  ["account_auth", "Account authentication (signed)"], ["order_validation", "Practice order validation (never placed)"],
  ["live_orders", "Live orders"]];
const STATE_LABEL: Record<string, string> = {
  ok: "OK", geo_restricted: "BLOCKED: location (HTTP 451)", forbidden: "BLOCKED (HTTP 403)", rate_limited: "rate limited",
  auth_failed: "key rejected", timeout: "timeout", unreachable: "unreachable", malformed: "bad response",
  exchange_error: "exchange error", rejected: "order refused", error: "error", blocked_locally: "refused locally",
  skipped: "skipped", not_applicable: "n/a", not_checked: "not checked", locked: "LOCKED" };

function CheckList({ checks }: { checks: Checks }) {
  return (
    <div className="small" style={{ marginTop: 4 }}>
      {CHECK_LABEL.map(([k, label]) => {
        const ch = checks[k] as Check;
        const cls = ch.ok ? "pill-ok" : ["skipped", "not_applicable", "not_checked", "locked"].includes(ch.state) ? "pill-off" : "pill-danger";
        return <div key={k} title={ch.message}><span className={`pill ${cls}`}>{STATE_LABEL[ch.state] ?? ch.state}</span> {label}
          {!ch.ok && !["skipped", "locked"].includes(ch.state) && <div className="muted">{ch.message}</div>}</div>;
      })}
      <div className="muted">Tested from {checks.origin}.</div>
    </div>
  );
}
type Env = { exchange: string; environment: string; host: string; label: string; note: string };
type Listing = { live_orders_unlocked: boolean; environments: Env[]; connections: Conn[] };

function errText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  return e instanceof Error ? e.message : String(e);
}

const yn = (v: boolean | null | undefined, good: boolean) =>
  v === null || v === undefined ? <span className="pill pill-off">not verified</span>
    : <span className={`pill ${v === good ? "pill-ok" : "pill-danger"}`}>{v ? "yes" : "no"}</span>;

function EnvPill({ c }: { c: { environment: string } }) {
  return <span className={`pill ${c.environment === "LIVE" ? "pill-danger" : "pill-warn"}`}>{c.environment}</span>;
}

function AddForm({ envs, onSaved }: { envs: Env[]; onSaved: () => void }) {
  const [exchange, setExchange] = useState("bybit");
  const [environment, setEnvironment] = useState("DEMO");
  const [label, setLabel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [confirm, setConfirm] = useState("");
  const [password, setPassword] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const env = envs.find((e) => e.exchange === exchange && e.environment === environment);

  async function save() {
    try {
      await apiPost("/api/crypto/connections", { exchange, environment, confirm_environment: confirm, label,
        api_key: apiKey, api_secret: apiSecret, password });
      setApiKey(""); setApiSecret(""); setPassword(""); setConfirm(""); setLabel("");
      setMsg("Saved (disabled). Run a test next.");
      onSaved();
    } catch (e) {
      setMsg(errText(e));
    }
  }

  return (
    <div className="card" style={{ padding: 12 }}>
      <div className="form-grid">
        <div className="form-row"><label>Exchange</label><select value={exchange} onChange={(e) => setExchange(e.target.value)}>
            <option value="bybit">Bybit</option><option value="binance">Binance</option>
          </select></div>
        <div className="form-row"><label>Environment</label><select value={environment} onChange={(e) => setEnvironment(e.target.value)}>
            <option value="DEMO">DEMO</option><option value="TESTNET">TESTNET</option><option value="LIVE">LIVE (real funds)</option>
          </select></div>
        <div className="form-row"><label>Label</label><input value={label} maxLength={48} onChange={(e) => setLabel(e.target.value)} /></div>
        <div className="form-row"><label>API key</label><input value={apiKey} autoComplete="off" spellCheck={false} onChange={(e) => setApiKey(e.target.value)} /></div>
        <div className="form-row"><label>API secret</label><input type="password" value={apiSecret} autoComplete="new-password" onChange={(e) => setApiSecret(e.target.value)} /></div>
        <div className="form-row"><label>Type the environment again</label><input value={confirm} placeholder={environment} onChange={(e) => setConfirm(e.target.value.toUpperCase())} /></div>
        <div className="form-row"><label>Your dashboard password</label><input type="password" value={password} autoComplete="current-password" onChange={(e) => setPassword(e.target.value)} /></div>
      </div>
      {env && <div className="muted small" style={{ marginTop: 6 }}>Host: <code>{env.host}</code>. {env.note}</div>}
      <div className="muted small">
        Create the key on the exchange with trading and reading only. Never enable withdrawals or transfers, and
        restrict it to the server&apos;s IP address. A demo or testnet key is a different key from a live key.
      </div>
      <div className="btn-row" style={{ marginTop: 8 }}>
        <button className={environment === "LIVE" ? "btn btn-sm btn-danger" : "btn btn-sm"}
          disabled={!label || !apiKey || !apiSecret || !password || confirm !== environment} onClick={save}>
          Save {environment} key
        </button>
        {msg && <span className="small">{msg}</span>}
      </div>
    </div>
  );
}

function ConnRow({ c, onChanged }: { c: Conn; onChanged: () => void }) {
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const p = c.permissions;

  async function run(fn: () => Promise<unknown>, done?: string) {
    setBusy(true);
    try {
      await fn();
      setMsg(done ?? null);
      onChanged();
    } catch (e) {
      setMsg(errText(e));
    } finally {
      setBusy(false);
    }
  }

  function askPassword(question: string): string | null {
    return window.prompt(`${question}\n\nEnter your dashboard password:`);
  }

  function toggle() {
    let password: string | null = null;
    if (c.environment === "LIVE") {
      password = askPassword(c.enabled ? "Disable this LIVE connection?"
        : "Enable this LIVE connection? This does not allow any order: real orders stay locked.");
      if (!password) return;
    }
    run(() => apiPut(`/api/crypto/connections/${c.id}`, { enabled: !c.enabled, password }));
  }

  function remove() {
    const password = askPassword(`Delete the ${c.environment} connection "${c.label}"? The key is erased.`);
    if (password) run(() => apiPost(`/api/crypto/connections/${c.id}/delete`, { password }));
  }

  return (
    <tr>
      <td>{c.exchange} <EnvPill c={c} /><div className="small">{c.label}</div><div className="muted small"><code>{c.host}</code></div></td>
      <td className="mono">{c.key_hint}</td>
      <td>
        <span className={`pill ${c.status === "OK" ? "pill-ok" : c.status === "UNTESTED" ? "pill-off" : "pill-danger"}`}>{c.status}</span>
        <div className="muted small">checked {formatDate(c.checked_at)}</div>
        {c.checks ? <CheckList checks={c.checks} /> : c.last_error && <div className="neg small">{c.last_error}</div>}
      </td>
      <td className="small">
        {p ? <>
          <div>Spot trading {yn(p.can_trade_spot, true)} · Derivatives {yn(p.can_trade_derivatives, true)}</div>
          <div>Withdrawals {yn(p.withdraw_enabled, false)} · IP restricted {yn(p.ip_restricted, true)}</div>
          {p.notes.map((n) => <div key={n} className="muted">{n}</div>)}
          {p.balances && p.balances.length > 0 && <div className="muted">Balances: {p.balances.map((b) => `${b.total} ${b.asset}`).join(", ")}</div>}
        </> : <span className="muted">test the connection to read its permissions</span>}
      </td>
      <td className="small">
        {c.live_gates ? c.live_gates.map((g) => (
          <div key={g.id}><span className={`pill ${g.ok ? "pill-ok" : "pill-danger"}`}>{g.ok ? "pass" : "fail"}</span> {g.detail}</div>
        )) : <span className="muted">practice account: never sends real orders</span>}
      </td>
      <td>
        <div className="btn-row">
          <button className="btn btn-sm" disabled={busy} onClick={() => run(() => apiPost(`/api/crypto/connections/${c.id}/test`), "Test finished.")}>Test</button>
          <button className="btn btn-sm btn-ghost" disabled={busy} onClick={toggle}>{c.enabled ? "Disable" : "Enable"}</button>
          <button className="btn btn-sm btn-ghost" disabled={busy} onClick={remove}>Delete</button>
        </div>
        {msg && <div className="small">{msg}</div>}
      </td>
    </tr>
  );
}

export default function ExchangeConnections() {
  const { data, error, reload } = useApi<Listing>("/api/crypto/connections");
  return (
    <div>
      <div className="page-header">
        <div className="page-title">Crypto Trading · Exchange Connections</div>
        {data && (data.live_orders_unlocked
          ? <span className="pill pill-danger">LIVE ORDERS UNLOCKED IN CODE</span>
          : <span className="pill pill-ok">Live orders locked</span>)}
      </div>
      <div className="notice">
        Demo / testnet and live keys are separate: each key works only with its own environment&apos;s host, and the
        same key cannot be saved twice. Keys are encrypted on the server and never shown again. Real orders are
        refused by the backend until live trading is reviewed and unlocked in code, and then only for a tested LIVE
        connection that cannot withdraw and is IP-restricted.
      </div>
      {error && <div className="error">{error}</div>}
      {data && (
        <>
          <div className="section-title" style={{ marginTop: 16 }}>Saved connections</div>
          {data.connections.length === 0 ? <div className="empty-state">No connection saved yet.</div> : (
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr><th>Connection</th><th>Key</th><th>Status</th><th>Permissions (reported by the exchange)</th><th>Live order gates</th><th /></tr></thead>
                <tbody>{data.connections.map((c) => <ConnRow key={c.id} c={c} onChanged={reload} />)}</tbody>
              </table>
            </div>
          )}
          <div className="section-title" style={{ marginTop: 16 }}>Add a connection</div>
          <AddForm envs={data.environments} onSaved={reload} />
        </>
      )}
    </div>
  );
}
