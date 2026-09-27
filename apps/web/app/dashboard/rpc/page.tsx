"use client";

import { useState } from "react";
import { Network } from "lucide-react";
import ConfirmButton from "@/components/ConfirmDialog";
import RuntimeApply from "@/components/RuntimeApply";
import { ErrorNotice, Loading, PageHeader, Section, Stat } from "@/components/ui";
import { apiDelete, apiPatch, apiPost, apiPut, ApiError } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

interface TestResult { status: string; detail: string; latency_ms: number | null; tested_at: string }
interface Provider {
  label: string; id: string | null; name: string; source: "env" | "dashboard"; provider_type: string;
  rpc_url: string; ws_url: string | null; enabled: boolean; priority: number; timeout_seconds: number | null;
  rate_limit_rps: number | null; notes: string | null; configured: boolean; decrypt_failed: boolean; last_test: TestResult | null;
  connected: boolean; healthy: "YES" | "NO" | "DEGRADED" | "UNKNOWN"; active: boolean; active_in: string[]; rate_limited_now: string[];
  success_rate: number | null; error_rate: number | null; successes: number; failures: number; rate_limited_count: number;
  latency_ms: number | null; last_success_at: string | null; last_failure_at: string | null; last_error: string | null; services: string[];
}
interface Listing {
  providers: Provider[]; active: string | null; provider_types: string[]; note: string;
  failovers: { service: string; from: string; to: string; reasons: string; at: string }[];
}

const yes = (b: boolean) => <span className={b ? "pill pill-ok" : "pill pill-off"}>{b ? "YES" : "NO"}</span>;
const health = (h: string) => <span className={h === "YES" ? "pill pill-ok" : h === "UNKNOWN" ? "pill pill-off" : h === "DEGRADED" ? "pill pill-warn" : "pill pill-danger"}>{h}</span>;
const testPill = (s: string) => <span className={s === "CONNECTED" ? "pill pill-ok" : s === "RATE_LIMITED" || s === "TIMEOUT" ? "pill pill-warn" : "pill pill-danger"}>{s}</span>;
const pct = (v: number | null) => (v === null ? "—" : `${(v * 100).toFixed(1)}%`);

const EMPTY = { name: "", provider_type: "alchemy", rpc_url: "", ws_url: "", priority: "150", timeout_seconds: "10", rate_limit_rps: "", notes: "", password: "" };

/** RPC & data providers: every Solana endpoint (dashboard-added and .env),
 * its real health from what the running services saw, test/add/edit/
 * reorder/disable — applied by the services without a restart. */
export default function RpcPage() {
  const { data, error, reload } = useApi<Listing>("/api/rpc/providers", undefined, { refreshMs: 5000 });
  const [form, setForm] = useState(EMPTY);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tests, setTests] = useState<Record<string, TestResult>>({});
  const [saved, setSaved] = useState(false);
  const [edit, setEdit] = useState<{ id: string; rpc_url: string; ws_url: string; password: string } | null>(null);

  async function run(fn: () => Promise<unknown>, ok: string) {
    setBusy(true); setErr(null); setMsg(null); setSaved(false);
    try {
      await fn();
      setMsg(ok);
      setSaved(true);
      await reload();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Request failed.");
    } finally {
      setBusy(false);
    }
  }

  async function add() {
    await run(async () => {
      const r = await apiPost<{ test: TestResult }>("/api/rpc/providers", {
        name: form.name, provider_type: form.provider_type, rpc_url: form.rpc_url, ws_url: form.ws_url || null,
        priority: Number(form.priority), timeout_seconds: form.timeout_seconds, rate_limit_rps: form.rate_limit_rps || null,
        notes: form.notes || null, password: form.password, enabled: true,
      });
      setTests((t) => ({ ...t, [`db:${form.name}`]: r.test }));
      setForm(EMPTY);
    }, "RPC added. Test result below; services load it on this configuration revision.");
  }

  async function test(p: Provider) {
    setBusy(true); setErr(null);
    try {
      const r = await apiPost<TestResult>(`/api/rpc/providers/${encodeURIComponent(p.id ?? p.label)}/test`);
      setTests((t) => ({ ...t, [p.label]: r }));
      await reload();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Test failed.");
    } finally {
      setBusy(false);
    }
  }

  function patch(p: Provider, body: Record<string, unknown>, ok: string) {
    return run(() => (p.source === "env"
      ? apiPut(`/api/rpc/providers/env/${encodeURIComponent(p.label)}`, body)
      : apiPatch(`/api/rpc/providers/${p.id}`, body)), ok);
  }

  if (error) return <ErrorNotice error={error} />;
  if (!data) return <Loading />;
  const active = data.providers.find((p) => p.label === data.active);
  return (
    <div>
      <PageHeader title="RPC & Data Providers" icon={<Network size={20} aria-hidden />}
        subtitle="Solana RPC / WebSocket endpoints. Changes here are applied by the running services on the next configuration revision — no restart, no .env edit." />
      <div className="stat-grid">
        <Stat label="Active RPC (decision engine)">{active ? `${active.name} (${active.rpc_url})` : "no successful request yet"}</Stat>
        <Stat label="Endpoints enabled">{data.providers.filter((p) => p.enabled).length} / {data.providers.length}</Stat>
        <Stat label="Rate-limited now">{data.providers.filter((p) => p.rate_limited_now.length).map((p) => p.name).join(", ") || "none"}</Stat>
      </div>
      {err && <ErrorNotice error={err} />}
      {msg && <div className="notice">{msg} {saved && <RuntimeApply inline />}</div>}

      <Section title="Providers (tried in priority order, lowest first)">
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Priority</th><th>Provider</th><th>Configured</th><th>Connected</th><th>Healthy</th><th>Active</th>
              <th>Success / errors</th><th>Latency</th><th>Last success / failure</th><th>Test</th><th>Controls</th></tr></thead>
            <tbody>{data.providers.map((p) => {
              const t = tests[p.label] ?? p.last_test;
              return (
                <tr key={p.label} className={p.enabled ? undefined : "muted"}>
                  <td>
                    <input aria-label={`priority of ${p.name}`} style={{ width: 64 }} defaultValue={p.priority} inputMode="numeric"
                      onBlur={(e) => { const v = Number(e.target.value); if (v && v !== p.priority) void patch(p, { priority: v }, `Priority of ${p.name} set to ${v}.`); }} />
                  </td>
                  <td>
                    <b>{p.name}</b> <span className="muted">{p.source === "env" ? ".env" : p.provider_type}</span>
                    <div className="mono muted">{p.rpc_url}{p.ws_url ? ` · ws ${p.ws_url}` : ""}</div>
                    {p.rate_limit_rps && <div className="muted">limit {p.rate_limit_rps} req/s · timeout {p.timeout_seconds}s</div>}
                    {p.decrypt_failed && <div className="neg">stored URL cannot be decrypted — re-enter it</div>}
                  </td>
                  <td>{yes(p.configured)}</td>
                  <td>{yes(p.connected)}</td>
                  <td>{health(p.healthy)}{p.rate_limited_now.length > 0 && <div className="pill pill-warn">RATE LIMITED</div>}</td>
                  <td>{yes(p.active)}{p.active_in.length > 0 && <div className="muted">{p.active_in.join(", ")}</div>}</td>
                  <td>{pct(p.success_rate)} ok · {pct(p.error_rate)} err<div className="muted">{p.successes} / {p.failures} · 429×{p.rate_limited_count}</div></td>
                  <td>{p.latency_ms !== null ? `${p.latency_ms} ms` : "—"}</td>
                  <td className="muted">{formatDate(p.last_success_at)}<br />{formatDate(p.last_failure_at)}{p.last_error && <div className="neg">{p.last_error}</div>}</td>
                  <td>
                    <button className="btn btn-sm" disabled={busy} onClick={() => test(p)}>TEST CONNECTION</button>
                    {t && <div>{testPill(t.status)} <span className="muted">{t.latency_ms !== null ? `${t.latency_ms} ms · ` : ""}{t.detail}</span></div>}
                  </td>
                  <td>
                    <div className="btn-row">
                      <button className="btn btn-ghost btn-sm" disabled={busy}
                        onClick={() => patch(p, { enabled: !p.enabled }, `${p.name} ${p.enabled ? "disabled" : "enabled"}.`)}>
                        {p.enabled ? "Disable" : "Enable"}
                      </button>
                      {p.source === "dashboard" && (
                        <>
                          <button className="btn btn-ghost btn-sm" onClick={() => setEdit({ id: p.id!, rpc_url: "", ws_url: "", password: "" })}>Change URL</button>
                          <ConfirmButton label="Delete" danger title={`Delete ${p.name}?`} body="It is removed from every service on the next revision."
                            onConfirm={() => run(() => apiDelete(`/api/rpc/providers/${p.id}`), `${p.name} deleted.`)} />
                        </>
                      )}
                    </div>
                    {edit?.id === p.id && (
                      <div className="form-grid" style={{ marginTop: 6 }}>
                        <input placeholder="new RPC URL (https://…)" value={edit.rpc_url} onChange={(e) => setEdit({ ...edit, rpc_url: e.target.value })} />
                        <input placeholder="new WebSocket URL (wss://…, optional)" value={edit.ws_url} onChange={(e) => setEdit({ ...edit, ws_url: e.target.value })} />
                        <input type="password" placeholder="admin password" autoComplete="current-password" value={edit.password}
                          onChange={(e) => setEdit({ ...edit, password: e.target.value })} />
                        <div className="btn-row">
                          <button className="btn btn-sm" disabled={busy || !edit.password || (!edit.rpc_url && !edit.ws_url)}
                            onClick={() => run(() => apiPatch(`/api/rpc/providers/${p.id}`, {
                              ...(edit.rpc_url ? { rpc_url: edit.rpc_url } : {}), ...(edit.ws_url ? { ws_url: edit.ws_url } : {}), password: edit.password,
                            }).then(() => setEdit(null)), `${p.name} URL changed and tested.`)}>Save URL</button>
                          <button className="btn btn-ghost btn-sm" onClick={() => setEdit(null)}>Cancel</button>
                        </div>
                      </div>
                    )}
                  </td>
                </tr>);
            })}</tbody>
          </table>
        </div>
        <p className="muted">{data.note} Healthy/connected come from real requests by the running services (last 5 min) or a
          successful test — never from the URL merely being saved. .env endpoints can be disabled or reordered here; their URLs
          stay in .env.</p>
      </Section>

      <Section title="ADD RPC">
        <div className="form-grid">
          <label>Provider name<input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="e.g. Alchemy main" /></label>
          <label>Type<select value={form.provider_type} onChange={(e) => setForm({ ...form, provider_type: e.target.value })}>
            {data.provider_types.map((t) => <option key={t} value={t}>{t}</option>)}</select></label>
          <label>Chain<input value="solana (mainnet)" disabled /></label>
          <label>RPC URL (full, with key)<input value={form.rpc_url} onChange={(e) => setForm({ ...form, rpc_url: e.target.value })} placeholder="https://solana-mainnet.g.alchemy.com/v2/…" autoComplete="off" /></label>
          <label>WebSocket URL (optional)<input value={form.ws_url} onChange={(e) => setForm({ ...form, ws_url: e.target.value })} placeholder="wss://…" autoComplete="off" /></label>
          <label>Priority (lower = first)<input value={form.priority} inputMode="numeric" onChange={(e) => setForm({ ...form, priority: e.target.value })} /></label>
          <label>Timeout (s)<input value={form.timeout_seconds} inputMode="decimal" onChange={(e) => setForm({ ...form, timeout_seconds: e.target.value })} /></label>
          <label>Rate limit (req/s, optional)<input value={form.rate_limit_rps} inputMode="decimal" onChange={(e) => setForm({ ...form, rate_limit_rps: e.target.value })} /></label>
          <label>Notes<input value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} /></label>
          <label>Admin password<input type="password" autoComplete="current-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></label>
          <div><button className="btn" disabled={busy || !form.name || !form.rpc_url || !form.password} onClick={add}>ADD RPC</button></div>
        </div>
        <p className="muted">The URL is validated, tested, encrypted at rest and never shown again (only scheme://host). The .env Helius
          primary has priority 100 and .env backups 200–400; a new provider defaults to 150 (after the primary).</p>
      </Section>

      <Section title="Failover events">
        {data.failovers.length === 0 ? <div className="muted">No failover recorded.</div> : (
          <table className="data-table">
            <thead><tr><th>When</th><th>Service</th><th>From → to</th><th>Why</th></tr></thead>
            <tbody>{data.failovers.map((f, i) => (
              <tr key={i}><td>{formatDate(f.at)}</td><td>{f.service}</td><td>{f.from} → {f.to}</td><td className="muted">{f.reasons || "recovered"}</td></tr>))}
            </tbody>
          </table>)}
      </Section>
    </div>
  );
}
