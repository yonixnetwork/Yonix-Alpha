"use client";

import { useState } from "react";
import { CheckCircle2, Power, ShieldAlert, ShieldCheck, XCircle } from "lucide-react";
import { ErrorNotice, Section } from "@/components/ui";
import { ApiError, apiPost } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

interface Check { code: string; label: string; status: "PASS" | "FAIL"; detail: string; action: string; kind: string }
interface LiveControl {
  state: "OBSERVATION_ONLY" | "PAPER" | "LIVE_ARMED_BLOCKED" | "LIVE_ELIGIBLE";
  label: string; summary: string; global_mode: string; checks: Check[]; failed: string[]; arming_ok: boolean;
  readiness_ok: boolean; checked_at: string; confirm_phrase: string; how_it_works: string[];
  locks_open: boolean; live_strategies: string[];
  last_change: { at: string; by: string; from: string; to: string; reason: string } | null;
  worker_enforcement: { ok: boolean; at: string; failed: string[] } | null;
}

const STATE_CLASS: Record<LiveControl["state"], string> = {
  OBSERVATION_ONLY: "pill pill-off", PAPER: "pill pill-ok", LIVE_ARMED_BLOCKED: "pill pill-warn", LIVE_ELIGIBLE: "pill pill-danger",
};

function message(e: unknown): string {
  if (e instanceof ApiError || e instanceof Error) return e.message;
  return String(e);
}

/** Dashboard-managed operating mode (GET/POST /api/control/live-control,
 * yonixalpha_core.live_control). Every check is decided by the server; the
 * buttons only send a request. */
export default function LiveControlPanel() {
  const q = useApi<LiveControl>("/api/control/live-control", undefined, { refreshMs: 15000, reloadOn: ["strategy.updated"] });
  const [password, setPassword] = useState("");
  const [phrase, setPhrase] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const d = q.data;
  const act = async (path: string, body: Record<string, string>) => {
    setBusy(true); setMsg(null);
    try {
      q.setData(await apiPost<LiveControl>(path, body));
      setPassword(""); setPhrase(""); setReason("");
    } catch (e) { setMsg(message(e)); } finally { setBusy(false); q.reload(); }
  };
  const live = d?.global_mode === "LIVE";
  return (
    <Section title="Operating mode and LIVE control">
      <ErrorNotice error={q.error} />
      {d && (
        <>
          <div className={live ? "card warn-card" : "card"} role="status" aria-live="polite">
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              {d.state === "LIVE_ELIGIBLE" ? <ShieldCheck size={16} aria-hidden /> : <ShieldAlert size={16} aria-hidden />}
              <b>Now:</b> <span className={STATE_CLASS[d.state]}>{d.label}</span>
              <span className="muted small">global mode {d.global_mode} · checked by the server {formatDate(d.checked_at)}</span>
            </div>
            <div style={{ marginTop: 6 }}>{d.summary}</div>
            {d.last_change && (
              <div className="muted small" style={{ marginTop: 6 }}>
                Last change: {d.last_change.from} → {d.last_change.to} by {d.last_change.by} at {formatDate(d.last_change.at)}: “{d.last_change.reason}”
              </div>)}
            {live && d.worker_enforcement && (
              <div className="muted small">Order worker re-check {formatDate(d.worker_enforcement.at)}: {d.worker_enforcement.ok ? "passing" : `failing (${d.worker_enforcement.failed.join(", ")})`}</div>)}
          </div>
          <div className="table-scroll" style={{ marginTop: 8 }}>
            <table className="data-table">
              <thead><tr><th>Check</th><th>Result</th><th>What the server found</th><th>What to do</th></tr></thead>
              <tbody>{d.checks.map((c) => (
                <tr key={c.code}>
                  <td>{c.label}<span className="muted small"> ({c.kind === "arming" ? "to arm" : "every order"})</span></td>
                  <td>{c.status === "PASS"
                    ? <span className="pill pill-ok"><CheckCircle2 size={12} aria-hidden /> pass</span>
                    : <span className="pill pill-danger"><XCircle size={12} aria-hidden /> fail</span>}</td>
                  <td className="small">{c.detail}</td>
                  <td className="muted small">{c.action}</td>
                </tr>))}</tbody>
            </table>
          </div>
          {!live ? (
            <div className="card" style={{ marginTop: 8 }}>
              <b>Arm LIVE trading</b>
              <p className="muted small">Real money. Needs every check above to pass, your password, the phrase {d.confirm_phrase} and a reason.
                Only strategies set to MANUAL or AUTO can trade live ({d.live_strategies.join(", ") || "none now"}).</p>
              <div className="btn-row">
                <input type="password" aria-label="Password" placeholder="Your password" value={password} autoComplete="current-password"
                  onChange={(e) => setPassword(e.target.value)} />
                <input aria-label="Confirmation phrase" placeholder={`Type ${d.confirm_phrase}`} value={phrase} onChange={(e) => setPhrase(e.target.value)} />
                <input aria-label="Reason" placeholder="Reason (recorded)" value={reason} maxLength={300} style={{ minWidth: 220 }}
                  onChange={(e) => setReason(e.target.value)} />
                <button className="btn btn-danger btn-sm" disabled={busy || !d.arming_ok || phrase !== d.confirm_phrase || reason.trim().length < 5 || !password}
                  title={d.arming_ok ? undefined : "Blocked: see the failing checks above"}
                  onClick={() => act("/api/control/live-control/arm", { password, confirm: phrase, reason })}>
                  <Power size={14} aria-hidden /> Arm LIVE
                </button>
              </div>
              {!d.arming_ok && <p className="small neg">LIVE cannot be armed now: {d.failed.length} check(s) fail. The server refuses it too.</p>}
            </div>
          ) : (
            <div className="card warn-card" style={{ marginTop: 8 }}>
              <b>Disarm LIVE</b>
              <p className="muted small">Returns to PAPER at once. Queued live buys are cancelled; open live positions keep their stops and exits; nothing is closed.</p>
              <div className="btn-row">
                <input aria-label="Reason" placeholder="Reason (optional)" value={reason} maxLength={300} onChange={(e) => setReason(e.target.value)} />
                <button className="btn btn-sm" disabled={busy} onClick={() => act("/api/control/live-control/disarm", { reason })}>
                  <Power size={14} aria-hidden /> Disarm LIVE (back to PAPER)
                </button>
              </div>
            </div>
          )}
          {msg && <ErrorNotice error={msg} />}
          <details style={{ marginTop: 8 }}>
            <summary className="small">How this works</summary>
            <ul className="small">{d.how_it_works.map((h) => <li key={h}>{h}</li>)}</ul>
          </details>
        </>
      )}
    </Section>
  );
}
