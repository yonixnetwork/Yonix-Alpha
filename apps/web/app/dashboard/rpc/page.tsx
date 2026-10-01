"use client";

import { useState } from "react";
import { Network } from "lucide-react";
import ConfirmButton from "@/components/ConfirmDialog";
import PlanHealth, { RolesPlan, ROLES } from "@/components/PlanHealth";
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
  auth_failed_now?: string[]; refused_methods?: string[]; unsupported_methods?: string[]; forbidden_count?: number;
  capabilities?: Record<string, { status: string; at: string | null; source: string; detail: string | null; latency_ms: number | null }>;
  tx_versions_seen?: Record<string, number>; rate_limited_by_method?: Record<string, number>;
  success_rate: number | null; error_rate: number | null; successes: number; failures: number; rate_limited_count: number;
  latency_ms: number | null; last_success_at: string | null; last_failure_at: string | null; last_error: string | null; services: string[];
  roles?: string[]; plan?: string | null;
}
interface Listing {
  providers: Provider[]; active: string | null; provider_types: string[]; note: string;
  failovers: { service: string; from: string; to: string; reasons: string; at: string }[];
  routing?: Record<string, { method: string; provider: string | null; ok: number; fail: number; last_error: string | null; at: string | null }[]>;
}

const yes = (b: boolean) => <span className={b ? "pill pill-ok" : "pill pill-off"}>{b ? "YES" : "NO"}</span>;
const health = (h: string) => <span className={h === "YES" ? "pill pill-ok" : h === "UNKNOWN" ? "pill pill-off" : h === "DEGRADED" ? "pill pill-warn" : "pill pill-danger"}>{h}</span>;
const testPill = (s: string) => <span className={s === "CONNECTED" ? "pill pill-ok" : s === "RATE_LIMITED" || s === "TIMEOUT" ? "pill pill-warn" : "pill pill-danger"}>{s}</span>;
const MATRIX_METHODS = ["getLatestBlockhash", "sendTransaction", "simulateTransaction", "getSignatureStatuses", "getTransaction",
  "getSignaturesForAddress", "getBalance", "getTokenAccountsByOwner", "getMultipleAccounts", "getAccountInfo"];
const capClass = (s?: string) => s === "SUPPORTED" ? "pill pill-ok" : s === "UNSUPPORTED" || s === "FORBIDDEN" ? "pill pill-danger"
  : s === "RATE_LIMITED" || s === "ERROR" ? "pill pill-warn" : "pill pill-off";
const pct = (v: number | null) => (v === null ? "—" : `${(v * 100).toFixed(1)}%`);

const EMPTY = { name: "", chain: "solana", provider_type: "alchemy", rpc_url: "", ws_url: "", priority: "150", timeout_seconds: "10",
  rate_limit_rps: "", notes: "", password: "", plan: "", roles: [] as string[] };
const CHAIN_LABEL: Record<string, string> = { solana: "Solana", bsc: "BSC (BNB Smart Chain, id 56)", robinhood: "Robinhood Chain (id 4663)" };
const PLACEHOLDER: Record<string, string> = {
  solana: "https://solana-mainnet.g.alchemy.com/v2/…", bsc: "https://bnb-mainnet.g.alchemy.com/v2/… (copy from your provider)",
  robinhood: "https://… Robinhood Chain mainnet URL from your provider",
};

interface EvmTest extends TestResult { logs_max_span?: number | null; head?: number | null; chain_id?: number | null }
interface EvmRow {
  label: string; id: string | null; name: string; source: "dashboard" | "env" | "public"; provider_type: string | null;
  rpc_url: string; enabled: boolean; priority: number; decrypt_failed: boolean; notes: string | null; last_test: EvmTest | null;
  live: { state: string; cooldown_s: number; last_error: string | null; ok: number; errors: number; rate_limited: number;
    latency_ms: number | null; unsupported_methods: string[]; reported_at?: number; logs_span?: number | null } | null;
  roles: string[]; plan: string | null; ws_url: string | null; rate_limit_rps: number | null;
}
interface EvmListing {
  chains: Record<string, { chain_id: number; endpoints: EvmRow[]; in_use: string[] }>;
  guide: { summary: string; note: string; providers: { name: string; chains: string[]; where: string; steps: string }[] };
  note: string;
}

function EvmProviders({ busy, run }: { busy: boolean; run: (fn: () => Promise<unknown>, ok: string) => Promise<void> }) {
  const { data, error, reload } = useApi<EvmListing>("/api/rpc/evm", undefined, { refreshMs: 10000 });
  const [tests, setTests] = useState<Record<string, EvmTest>>({});
  const [testing, setTesting] = useState<string | null>(null);
  if (error) return <ErrorNotice error={error} />;
  if (!data) return <Loading />;
  async function test(r: EvmRow) {
    setTesting(r.label);
    try {
      const t = await apiPost<EvmTest>(`/api/rpc/providers/${encodeURIComponent(r.id ?? r.label)}/test`);
      setTests((x) => ({ ...x, [r.label]: t }));
      await reload();
    } catch (e) {
      setTests((x) => ({ ...x, [r.label]: { status: "ERROR", detail: e instanceof ApiError ? e.message : "test failed", latency_ms: null, tested_at: "" } }));
    } finally { setTesting(null); }
  }
  const change = (r: EvmRow, body: Record<string, unknown>, ok: string) => run(() => (r.source === "dashboard"
    ? apiPatch(`/api/rpc/providers/${r.id}`, body) : apiPut(`/api/rpc/evm/${encodeURIComponent(r.label)}`, body)).then(reload), ok);
  return (
    <>
      {Object.entries(data.chains).map(([chain, c]) => (
        <Section key={chain} title={`${CHAIN_LABEL[chain] ?? chain} endpoints (data-evm and copy trading)`}>
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>Priority</th><th>Endpoint</th><th>Enabled</th><th>Live state</th><th>Requests</th><th>Logs span</th><th>Test</th><th>Controls</th></tr></thead>
              <tbody>{c.endpoints.map((r) => {
                const t = tests[r.label] ?? r.last_test;
                return (
                  <tr key={r.label} className={r.enabled ? undefined : "muted"}>
                    <td><input aria-label={`priority of ${r.name}`} style={{ width: 64 }} defaultValue={r.priority} inputMode="numeric"
                      onBlur={(e) => { const v = Number(e.target.value); if (v && v !== r.priority) void change(r, { priority: v }, `Priority of ${r.name} set to ${v}.`); }} /></td>
                    <td><b>{r.name}</b> <span className="muted">{r.source === "dashboard" ? r.provider_type : r.source === "env" ? ".env" : "built-in public"}</span>
                      <div className="mono muted">{r.rpc_url}{r.ws_url ? ` · ws ${r.ws_url}` : ""}</div>
                      {r.rate_limit_rps ? <div className="muted">limit {r.rate_limit_rps} req/s</div> : null}
                      <RolesPlan roles={r.roles} plan={r.plan} disabled={busy}
                        onSave={(b) => change(r, b, `Roles / plan of ${r.name} saved.`)} />
                      {r.decrypt_failed && <div className="neg">stored URL cannot be decrypted — re-enter it</div>}</td>
                    <td>{yes(r.enabled)}</td>
                    <td>{r.live ? <><span className={r.live.state === "OK" ? "pill pill-ok" : r.live.state === "COOLDOWN" ? "pill pill-warn" : "pill pill-danger"}>{r.live.state}</span>
                      {r.live.cooldown_s > 0 && <div className="muted">cooling down {r.live.cooldown_s}s</div>}
                      {r.live.unsupported_methods.length > 0 && <div className="neg">refuses: {r.live.unsupported_methods.join(", ")}</div>}
                      {r.live.last_error && <div className="muted">{r.live.last_error}</div>}</>
                      : <span className="muted small">not used yet</span>}</td>
                    <td>{r.live ? <>{r.live.ok} ok · {r.live.errors} err<div className="muted">429×{r.live.rate_limited}{r.live.latency_ms !== null ? ` · ${r.live.latency_ms} ms` : ""}</div></> : "—"}</td>
                    <td>{r.live?.logs_span ? <>{r.live.logs_span} blocks<div className="muted small">served lately</div></>
                      : t?.logs_max_span ? `${t.logs_max_span} blocks` : t ? <span className="neg">none</span> : "—"}</td>
                    <td><button className="btn btn-sm" disabled={busy || testing === r.label} onClick={() => test(r)}>{testing === r.label ? "TESTING…" : "TEST CONNECTION"}</button>
                      {t && <div>{testPill(t.status)} <span className="muted small">{t.detail}</span></div>}</td>
                    <td><div className="btn-row">
                      <button className="btn btn-ghost btn-sm" disabled={busy}
                        onClick={() => change(r, { enabled: !r.enabled }, `${r.name} ${r.enabled ? "disabled" : "enabled"}.`)}>{r.enabled ? "Disable" : "Enable"}</button>
                      {r.source === "dashboard" && <ConfirmButton label="Delete" danger title={`Delete ${r.name}?`} body="Removed from data-evm and copy trading on the next revision."
                        onConfirm={() => run(() => apiDelete(`/api/rpc/providers/${r.id}`).then(reload), `${r.name} deleted.`)} />}
                    </div></td>
                  </tr>);
              })}</tbody>
            </table>
          </div>
        </Section>))}
      <p className="muted">{data.note}</p>
      <Section title="Where to get an RPC — one provider for Solana, BSC and Robinhood Chain">
        <p className="small">{data.guide.summary}</p>
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Provider</th><th>Chains</th><th>Where</th><th>How</th></tr></thead>
            <tbody>{data.guide.providers.map((g) => (
              <tr key={g.name}><td><b>{g.name}</b></td><td>{g.chains.map((c) => CHAIN_LABEL[c] ?? c).join(", ")}</td>
                <td><a href={g.where} target="_blank" rel="noreferrer noopener">{g.where.replace("https://", "")}</a></td><td className="small">{g.steps}</td></tr>))}
            </tbody>
          </table>
        </div>
        <p className="muted small">{data.guide.note}</p>
      </Section>
    </>
  );
}

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
  const [addResult, setAddResult] = useState<TestResult | null>(null);

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
        name: form.name, chain: form.chain, provider_type: form.provider_type, rpc_url: form.rpc_url,
        ws_url: form.ws_url || null, roles: form.roles, plan: form.plan || null,
        priority: Number(form.priority), timeout_seconds: form.timeout_seconds, rate_limit_rps: form.rate_limit_rps || null,
        notes: form.notes || null, password: form.password, enabled: true,
      });
      setTests((t) => ({ ...t, [`db:${form.name}`]: r.test }));
      setAddResult(r.test);
      setForm(EMPTY);
    }, "RPC added. Services load it on this configuration revision.");
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
        subtitle="RPC endpoints for Solana, BSC and Robinhood Chain. Changes here are applied by the running services on the next configuration revision — no restart, no .env edit." />
      <div className="stat-grid">
        <Stat label="Active RPC (decision engine)">{active ? `${active.name} (${active.rpc_url})` : "no successful request yet"}</Stat>
        <Stat label="Endpoints enabled">{data.providers.filter((p) => p.enabled).length} / {data.providers.length}</Stat>
        <Stat label="Rate-limited now">{data.providers.filter((p) => p.rate_limited_now.length).map((p) => p.name).join(", ") || "none"}</Stat>
      </div>
      {err && <ErrorNotice error={err} />}
      {msg && <div className="notice">{msg} {saved && <RuntimeApply inline />}</div>}
      <PlanHealth />

      <Section title="Solana providers (tried in priority order, lowest first)">
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
                    <RolesPlan roles={p.roles ?? []} plan={p.plan ?? null} disabled={busy}
                      onSave={(b) => void patch(p, b, `Roles / plan of ${p.name} saved.`)} />
                    {p.decrypt_failed && <div className="neg">stored URL cannot be decrypted — re-enter it</div>}
                  </td>
                  <td>{yes(p.configured)}</td>
                  <td>{yes(p.connected)}</td>
                  <td>{health(p.healthy)}{p.rate_limited_now.length > 0 && <div className="pill pill-warn">RATE LIMITED</div>}
                    {(p.auth_failed_now ?? []).length > 0 && <div className="pill pill-danger">AUTHENTICATION FAILED</div>}
                    {(p.refused_methods ?? []).length > 0 && <div className="muted">refuses: {(p.refused_methods ?? []).join(", ")}</div>}</td>
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
          <label>Chain<select value={form.chain} onChange={(e) => setForm({ ...form, chain: e.target.value })}>
            {Object.entries(CHAIN_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
          <label>RPC URL (full, with key)<input value={form.rpc_url} onChange={(e) => setForm({ ...form, rpc_url: e.target.value })} placeholder={PLACEHOLDER[form.chain]} autoComplete="off" /></label>
          <label>WebSocket URL (optional{form.chain === "solana" ? "" : "; stored for mempool / sequencer streaming"})
            <input value={form.ws_url} onChange={(e) => setForm({ ...form, ws_url: e.target.value })} placeholder="wss://…" autoComplete="off" /></label>
          <label>Plan (as named by the provider, optional)<input value={form.plan} maxLength={64} placeholder="e.g. Free, Growth, Pay As You Go"
            onChange={(e) => setForm({ ...form, plan: e.target.value })} /></label>
          <fieldset><legend className="small">Roles (none ticked = every role)</legend>
            {ROLES.map((x) => (
              <label key={x} className="small" style={{ marginRight: 8 }}><input type="checkbox" checked={form.roles.includes(x)}
                onChange={(e) => setForm({ ...form, roles: e.target.checked ? [...form.roles, x] : form.roles.filter((y) => y !== x) })} /> {x.replaceAll("_", " ").toLowerCase()}</label>))}
          </fieldset>
          <label>Priority (lower = first)<input value={form.priority} inputMode="numeric" onChange={(e) => setForm({ ...form, priority: e.target.value })} /></label>
          <label>Timeout (s)<input value={form.timeout_seconds} inputMode="decimal" onChange={(e) => setForm({ ...form, timeout_seconds: e.target.value })} /></label>
          <label>Rate limit (req/s, optional)<input value={form.rate_limit_rps} inputMode="decimal" onChange={(e) => setForm({ ...form, rate_limit_rps: e.target.value })} /></label>
          <label>Notes<input value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} /></label>
          <label>Admin password<input type="password" autoComplete="current-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></label>
          <div><button className="btn" disabled={busy || !form.name || !form.rpc_url || !form.password} onClick={add}>ADD RPC</button></div>
        </div>
        {addResult && <div>Test of the new endpoint: {testPill(addResult.status)} <span className="muted">{addResult.detail}</span></div>}
        <p className="muted">The URL is validated, tested, encrypted at rest and never shown again (only scheme://host). Solana: the .env
          primary has priority 100 and .env backups 200–400; a new provider defaults to 150. BSC / Robinhood: the chain id must match
          and eth_getLogs must be served (discovery reads launchpad events); a new provider defaults to 150, before .env (500+) and
          the built-in public endpoints (900+).</p>
      </Section>

      <EvmProviders busy={busy} run={run} />

      <Section title="Capability matrix — what each provider actually serves">
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Provider</th><th>Healthy</th><th>Latency</th><th>429s</th><th>Tx versions seen</th>
              {MATRIX_METHODS.map((m) => <th key={m}><code>{m}</code></th>)}</tr></thead>
            <tbody>{data.providers.map((p) => (
              <tr key={p.label}>
                <td><strong>{p.name}</strong><div className="muted">{p.rpc_url}</div></td>
                <td>{health(p.healthy)}</td>
                <td>{p.latency_ms === null ? "—" : `${p.latency_ms} ms`}</td>
                <td title={Object.entries(p.rate_limited_by_method ?? {}).map(([m, n]) => `${m}: ${n}`).join("\n")}>{p.rate_limited_count}</td>
                <td>{Object.keys(p.tx_versions_seen ?? {}).length === 0 ? "—"
                  : Object.entries(p.tx_versions_seen ?? {}).map(([v, n]) => `v${v}: ${n}`).join(", ")}</td>
                {MATRIX_METHODS.map((m) => {
                  const c = p.capabilities?.[m];
                  return <td key={m} title={c ? `${c.source}${c.at ? ` · ${formatDate(c.at)}` : ""}${c.detail ? ` · ${c.detail}` : ""}` : "not observed yet"}>
                    <span className={capClass(c?.status)}>{c?.status ?? "UNKNOWN"}</span></td>;
                })}
              </tr>))}
            </tbody>
          </table>
        </div>
        <p className="muted">From real requests made by the services and from the TEST button&apos;s read-only probe (it never
          sends a transaction; sendTransaction is known only from real trades). A provider that answers &quot;method not
          available&quot; is not asked for that method again for 6 hours; every getTransaction declares transaction version 1.
          Execution requests (quote state, blockhash, simulate, send, confirm) are critical; history and analytics
          lookups are background work that is dropped, not queued, while a provider is rate-limited.</p>
      </Section>

      <Section title="Request routing — which endpoint served each request type">
        {Object.keys(data.routing ?? {}).length === 0 ? <div className="muted">No service has reported yet.</div> : (
          <table className="data-table">
            <thead><tr><th>Service</th><th>Method</th><th>Served by</th><th>OK / failed</th><th>Last error</th></tr></thead>
            <tbody>{Object.entries(data.routing ?? {}).flatMap(([service, rows]) => rows.map((r) => (
              <tr key={`${service}-${r.method}`}><td>{service}</td><td><code>{r.method}</code></td><td>{r.provider ?? "—"}</td>
                <td>{r.ok} / {r.fail}</td><td className="muted">{r.last_error ?? ""}</td></tr>)))}
            </tbody>
          </table>)}
        <p className="muted">Each request type goes to the first usable endpoint in priority order. A provider that answers
          HTTP 401/403 for a method is skipped for that method for 10 minutes; if it refuses a basic request it is marked
          AUTHENTICATION FAILED and skipped for everything until its key/URL is fixed or a request succeeds.</p>
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
