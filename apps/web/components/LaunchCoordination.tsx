"use client";

import { useState } from "react";
import { ErrorNotice, Loading, Section } from "@/components/ui";
import { apiDelete, apiPost, apiPut } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;

const ACTION_CLASS: Record<string, string> = {
  NO_TRADE: "pill pill-danger", MANUAL_APPROVAL: "pill pill-warn", REDUCE_SIZE: "pill pill-warn", NONE: "pill pill-ok",
};
const STATUS_CLASS: Record<string, string> = {
  FLAG: "pill pill-danger", PASS: "pill pill-ok", UNKNOWN: "pill pill-off", NOT_CONFIGURED: "pill pill-off", NOT_APPLICABLE: "pill pill-off",
};
export const DETECTION_LABEL: Record<string, string> = {
  LAUNCH_BLOCK_BUNDLE: "Bought in the launch block",
  NEAR_SIMULTANEOUS_BUYERS: "Near-simultaneous buyers",
  DECLARED_EXEMPTIONS: "Declared snipe-tax exemptions",
  PRIVILEGED_BUYERS: "Privileged (tax-exempt) buyers",
  CREATOR_BOUGHT: "Creator-linked wallet bought",
  CREATOR_FUNDED_BUYERS: "Creator-funded buyers",
  COMMON_FUNDER: "Common funder",
  FRESH_WALLET_CLUSTER: "Fresh-wallet cluster",
  WINDOW_SUPPLY_CONCENTRATION: "Supply held by window buyers",
  SINGLE_WALLET_CONCENTRATION: "Largest window buyer's share",
  ABNORMAL_INITIAL_OWNERSHIP: "Abnormal initial ownership",
  COORDINATED_EXIT: "Coordinated exit",
  WINDOW_NOT_OBSERVED: "Launch window not observed",
  COORDINATION_DATA_UNAVAILABLE: "Chain data unavailable",
};
const short = (a: string | null | undefined) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : "—");
const share = (v: any) => (v === null || v === undefined || v === "" ? "—" : `${(Number(v) * 100).toFixed(2)}%`);

/** One-cell summary for token tables. */
export function CoordinationPill({ action, status }: { action?: string | null; status?: string | null }) {
  if (!status) return <span className="pill pill-off">not assessed</span>;
  if (status === "NO_COORDINATION_DETECTED") return <span className="pill pill-ok" title="None of the checks fired on the data available; not a safety verdict">none detected</span>;
  return <span className={ACTION_CLASS[action ?? ""] ?? "pill pill-off"}>{(action ?? "?").replaceAll("_", " ")}</span>;
}

/** Full assessment of one token, with the operator approval for MANUAL_APPROVAL. */
export function CoordinationDetail({ chain, token }: { chain: string; token: string }) {
  const { data, error, loading, reload } = useApi<J>(`/api/evm/tokens/${chain}/${token}`, undefined, { refreshMs: 15000 });
  const [msg, setMsg] = useState<string | null>(null);
  if (error) return <ErrorNotice error={error} />;
  if (loading && !data) return <Loading />;
  const t = data?.token ?? {};
  const c: J | null = t.coordination;
  const appr: J | null = t.coordination_approval;
  const approve = async () => {
    try { await apiPost(`/api/evm/tokens/${chain}/${token}/coordination-approval`); setMsg("Approved for paper entries until it expires or the findings change."); reload(); }
    catch (e) { setMsg(String((e as Error).message)); }
  };
  const revoke = async () => {
    try { await apiDelete(`/api/evm/tokens/${chain}/${token}/coordination-approval`); setMsg("Approval revoked."); reload(); }
    catch (e) { setMsg(String((e as Error).message)); }
  };
  if (!c) return <p className="muted small">Not assessed yet: the check runs with the safety pass while the token is trading in an entry category.</p>;
  const approvalValid = appr && appr.fingerprint === c.fingerprint && new Date(appr.expires_at) > new Date();
  return (
    <div>
      <p className="small">
        <CoordinationPill action={c.action} status={c.status} />{" "}
        <span className="muted">{c.status.replaceAll("_", " ")} · assessed {formatDate(t.coordination_at)} · window {c.window?.window_seconds} s,
          {" "}{c.window?.buyers ?? 0} buyer(s){c.window?.launch_block ? `, launch block ${c.window.launch_block}` : ""}</span>
      </p>
      {c.findings.length > 0 && (
        <ul className="small">
          {c.findings.map((f: J) => (
            <li key={f.code}><span className={ACTION_CLASS[f.action] ?? "pill pill-off"}>{f.action.replaceAll("_", " ")}</span>{" "}
              <strong>{DETECTION_LABEL[f.code] ?? f.code}</strong>: {f.message}</li>))}
        </ul>
      )}
      {c.action === "MANUAL_APPROVAL" && (
        <div className="btn-row">
          {approvalValid
            ? <><span className="small">Approved by {appr!.by} until {formatDate(appr!.expires_at)}</span>
                <button className="btn btn-ghost btn-sm" onClick={revoke}>Revoke approval</button></>
            : <button className="btn btn-sm" onClick={approve}>Approve paper entries for these findings</button>}
        </div>
      )}
      {msg && <p className="small">{msg}</p>}
      <div className="table-scroll">
        <table className="data-table">
          <thead><tr><th>Check</th><th>Result</th><th>Value</th><th>Limit</th><th>Detail</th><th>Source</th></tr></thead>
          <tbody>
            {c.checks.map((ch: J) => (
              <tr key={ch.check}>
                <td className="small">{DETECTION_LABEL[ch.check] ?? ch.check}</td>
                <td><span className={STATUS_CLASS[ch.status] ?? "pill pill-off"}>{ch.status.replaceAll("_", " ")}</span></td>
                <td className="mono small">{ch.value ?? "—"}</td>
                <td className="mono small">{ch.limit ?? "—"}</td>
                <td className="small">{ch.message}{ch.wallets?.length ? <span className="muted" title={ch.wallets.join("\n")}> ({ch.wallets.map(short).join(", ")})</span> : null}</td>
                <td className="small muted">{ch.source}</td>
              </tr>))}
          </tbody>
        </table>
      </div>
      {c.wallets?.length > 0 && (
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Window buyer</th><th>Roles</th><th>First buy</th><th>Block</th><th>Holds (of supply)</th><th>Sells</th><th>First funder</th><th>Tx count</th></tr></thead>
            <tbody>
              {c.wallets.map((w: J) => (
                <tr key={w.wallet}>
                  <td className="mono small" title={w.wallet}>{short(w.wallet)}</td>
                  <td className="small">{w.roles.length ? w.roles.map((r: string) => r.replaceAll("_", " ")).join(", ") : "—"}</td>
                  <td className="small">{formatDate(w.first_at)}</td>
                  <td className="mono small">{w.first_block ?? "—"}</td>
                  <td className="mono small">{share(w.held_share)}</td>
                  <td>{w.sells}</td>
                  <td className="mono small" title={w.funder ?? ""}>{w.funder ? short(w.funder) : <span className="muted">unknown</span>}</td>
                  <td>{w.nonce ?? <span className="muted">unknown</span>}</td>
                </tr>))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted small">{c.window?.basis}. {c.note}.</p>
    </div>
  );
}

/** How often each detection fired per launchpad, and which checks lacked data. */
export function CoordinationSummary({ chain }: { chain: string }) {
  const { data, error } = useApi<J>("/api/evm/coordination/summary", { chain, hours: 24 }, { refreshMs: 60000 });
  if (error) return <ErrorNotice error={error} />;
  if (!data) return null;
  if (data.launchpads.length === 0) return <p className="muted small">No token assessed in the last 24 hours on this chain.</p>;
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead><tr><th>Launchpad</th><th>Assessed (24h)</th><th>Detected</th><th>Actions</th><th>Detections</th><th>Checks without data</th></tr></thead>
        <tbody>
          {data.launchpads.map((l: J) => (
            <tr key={l.launchpad}>
              <td>{l.launchpad}</td>
              <td>{l.assessed}</td>
              <td>{l.status.COORDINATION_DETECTED ?? 0}</td>
              <td className="small">{Object.entries(l.action).map(([k, v]) => `${k.replaceAll("_", " ")} ${v}`).join(" · ")}</td>
              <td className="small">{Object.entries(l.detections).map(([k, v]) => `${DETECTION_LABEL[k] ?? k} ${v}`).join(" · ") || "—"}</td>
              <td className="small muted">{Object.entries(l.unknown_checks).map(([k, v]) => `${DETECTION_LABEL[k] ?? k} ${v}`).join(" · ") || "—"}</td>
            </tr>))}
        </tbody>
      </table>
    </div>
  );
}

const NUMBERS: [string, string][] = [
  ["window_seconds", "Launch window (seconds)"], ["launch_block_buyers_max", "Launch-block buyers allowed (excl. creator)"],
  ["simultaneous_seconds", "Near-simultaneous span (s)"], ["simultaneous_buyers_max", "Near-simultaneous buyers allowed"],
  ["fresh_nonce_max", "Fresh wallet: at most N transactions"], ["fresh_buyers_max", "Fresh window buyers allowed"],
  ["common_funder_min_wallets", "Wallets sharing a funder to flag"], ["window_supply_share_max", "Max supply held by window buyers (0-1)"],
  ["single_wallet_share_max", "Max supply held by one window buyer (0-1)"], ["ownership_tolerance_share", "Initial-ownership tolerance (0-1)"],
  ["exit_wallets_min", "Window buyers selling together to flag"], ["exit_seconds", "Coordinated-exit span (s)"],
  ["reduce_size_factor", "REDUCE SIZE multiplier (0-1)"], ["max_wallet_lookups", "Wallets looked up on chain per launch"],
  ["approval_minutes", "Manual approval lasts (minutes)"],
];

export function CoordinationSettings() {
  const { data, error, reload } = useApi<J>("/api/evm/coordination-settings");
  const [draft, setDraft] = useState<J | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  if (error) return <ErrorNotice error={error} />;
  if (!data) return null;
  const vals = draft ?? data.settings;
  const set = (k: string, v: any) => setDraft({ ...vals, [k]: v });
  const save = async () => {
    try { await apiPut("/api/evm/coordination-settings", vals); setMsg("Saved. Applied on the next safety pass and copy buy."); setDraft(null); reload(); }
    catch (e) { setMsg(String((e as Error).message)); }
  };
  return (
    <div>
      <p className="muted small">{data.note}. NO TRADE blocks the entry, MANUAL APPROVAL waits for you, REDUCE SIZE multiplies the paper size, NONE only reports.</p>
      <div className="btn-row">
        <label className="small"><input type="checkbox" checked={!!vals.enabled} onChange={(e) => set("enabled", e.target.checked)} /> Check enabled</label>
        <label className="small"><input type="checkbox" checked={!!vals.funding_lookups} onChange={(e) => set("funding_lookups", e.target.checked)} /> Funding lookups (block explorer)</label>
      </div>
      <div className="form-grid">
        {data.detections.map((code: string) => (
          <label key={code} className="small">{DETECTION_LABEL[code] ?? code}
            <select style={{ display: "block" }} value={vals.actions[code]} onChange={(e) => set("actions", { ...vals.actions, [code]: e.target.value })}>
              {data.actions.map((a: string) => <option key={a} value={a}>{a.replaceAll("_", " ")}</option>)}
            </select></label>))}
      </div>
      <div className="form-grid">
        {NUMBERS.map(([k, label]) => (
          <label key={k} className="small">{label}
            <input style={{ display: "block" }} value={vals[k]} inputMode="decimal" onChange={(e) => set(k, e.target.value)} /></label>))}
        <label className="small">Ignored funders (exchanges, bridges; comma-separated)
          <input value={(vals.ignore_funders ?? []).join(",")} onChange={(e) => set("ignore_funders", e.target.value.split(",").map((s) => s.trim()).filter(Boolean))} /></label>
      </div>
      <div className="btn-row"><button className="btn btn-sm" disabled={!draft} onClick={save}>Save coordination settings</button>
        <button className="btn btn-ghost btn-sm" onClick={() => setDraft({ ...data.defaults })}>Reset to defaults</button></div>
      {msg && <p className="small">{msg}</p>}
    </div>
  );
}

export function CoordinationPanel({ chain }: { chain: string }) {
  const [open, setOpen] = useState(false);
  return (
    <Section title="Launch-window coordination">
      <p className="muted small">Who bought in each launch window and what they did next: bundles in the launch block, snipe-tax exemptions (Pons V2),
        creator-linked and creator-funded wallets, common funders, fresh wallets, supply concentration, abnormal initial ownership and coordinated exits.
        Pons is an active venue, not a safe one. Click a token above for its assessment.</p>
      <CoordinationSummary chain={chain} />
      <button className="btn btn-ghost btn-sm" onClick={() => setOpen(!open)}>{open ? "Hide settings" : "Settings"}</button>
      {open && <CoordinationSettings />}
    </Section>
  );
}
