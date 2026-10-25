"use client";

import { useState } from "react";
import { CheckCircle2, ShieldAlert, ShieldCheck, XCircle } from "lucide-react";
import ConfirmButton from "@/components/ConfirmDialog";
import { ErrorNotice, Section } from "@/components/ui";
import { ApiError, apiPost } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

interface Check { code: string; label: string; status: "PASS" | "FAIL"; detail: string; action: string; kind: string }
interface LiveControl {
  state: "OBSERVATION_ONLY" | "PAPER" | "LIVE_ARMED_BLOCKED" | "LIVE_ELIGIBLE";
  label: string; summary: string; global_mode: string; checks: Check[]; failed: string[]; arming_ok: boolean;
  readiness_ok: boolean; checked_at: string; how_it_works: string[]; toggle: { on: boolean };
  locks_open: boolean; live_strategies: string[];
  last_change: { at: string; by: string; from: string; to: string; reason: string } | null;
  worker_enforcement: { ok: boolean; at: string; failed: string[] } | null;
}
interface Refusal { at: string; message: string; failed: Check[] }

const STATE_CLASS: Record<LiveControl["state"], string> = {
  OBSERVATION_ONLY: "pill pill-off", PAPER: "pill pill-ok", LIVE_ARMED_BLOCKED: "pill pill-warn", LIVE_ELIGIBLE: "pill pill-danger",
};

function message(e: unknown): string {
  if (e instanceof ApiError || e instanceof Error) return e.message;
  return String(e);
}

/** The LIVE toggle (GET/POST /api/control/live-control, yonixalpha_core.live_control).
 * OFF = no new real-money entries. ON asks for confirmation, then the server runs
 * every mandatory check and switches only if all pass. The page shows the state
 * the server stored, never an assumed one. */
export default function LiveControlPanel() {
  const q = useApi<LiveControl>("/api/control/live-control", undefined, { refreshMs: 15000, reloadOn: ["strategy.updated"] });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  const d = q.data;
  const on = d?.toggle.on ?? false;

  const switchOn = async () => {
    setMsg(null);
    try {
      q.setData(await apiPost<LiveControl>("/api/control/live-control/arm", { confirm: true }));
      setRefusal(null);
    } catch (e) {
      const detail = e instanceof ApiError ? (e.detail as { message?: string; failed?: Check[] } | undefined) : undefined;
      if (e instanceof ApiError && e.status === 409 && detail?.failed) {
        setRefusal({ at: new Date().toISOString(), message: detail.message ?? e.message, failed: detail.failed });
      } else {
        setMsg(message(e));
      }
    } finally {
      q.reload();
    }
  };
  const switchOff = async () => {
    setBusy(true); setMsg(null);
    try {
      q.setData(await apiPost<LiveControl>("/api/control/live-control/disarm", { reason: "switched off with the dashboard toggle" }));
    } catch (e) { setMsg(message(e)); } finally { setBusy(false); q.reload(); }
  };

  return (
    <Section title="LIVE trading">
      <ErrorNotice error={q.error} />
      {d && (
        <>
          <div className={on ? "card warn-card" : "card"} role="status" aria-live="polite">
            <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
              <b>LIVE trading</b>
              <div className="btn-row" role="group" aria-label="LIVE trading">
                <button type="button" className={!on ? "btn btn-sm" : "btn btn-ghost btn-sm"} aria-pressed={!on}
                  disabled={!on || busy} onClick={switchOff}>
                  OFF
                </button>
                <ConfirmButton
                  label="ON"
                  className={on ? "btn btn-danger btn-sm" : "btn btn-ghost btn-sm"}
                  ariaLabel="Switch LIVE trading on"
                  disabled={on || busy}
                  danger
                  title="Switch LIVE trading on?"
                  confirmLabel="Yes, request LIVE"
                  body={
                    <>
                      <p><b>Real funds in the trading wallet may be used.</b> Strategies set to MANUAL or AUTO
                        ({d.live_strategies.join(", ") || "none now"}) can then open real positions when the gate approves a token.</p>
                      <p>After you confirm, the server runs every safety check. If any check fails, LIVE stays OFF and each failing
                        check is listed with what to do. Switching OFF later stops new live entries at once and closes nothing.</p>
                      {!d.arming_ok && <p className="neg small">{d.failed.length} check(s) fail right now; the server will refuse.</p>}
                    </>
                  }
                  onConfirm={switchOn}
                />
              </div>
              <span className="muted small">{on ? "ON" : "OFF"}, as stored on the server</span>
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", marginTop: 8 }}>
              {d.state === "LIVE_ELIGIBLE" ? <ShieldCheck size={16} aria-hidden /> : <ShieldAlert size={16} aria-hidden />}
              <b>Effective mode:</b> <span className={STATE_CLASS[d.state]}>{d.label}</span>
              <span className="muted small">global mode {d.global_mode} · checked by the server {formatDate(d.checked_at)}</span>
            </div>
            <div style={{ marginTop: 6 }}>{d.summary}</div>
            {d.last_change && (
              <div className="muted small" style={{ marginTop: 6 }}>
                Last change: {d.last_change.from} → {d.last_change.to} by {d.last_change.by} at {formatDate(d.last_change.at)}: “{d.last_change.reason}”
              </div>)}
            {on && d.worker_enforcement && (
              <div className="muted small">Order worker re-check {formatDate(d.worker_enforcement.at)}: {d.worker_enforcement.ok ? "passing" : `failing (${d.worker_enforcement.failed.join(", ")}): no new live order is sent`}</div>)}
          </div>
          {refusal && !on && (
            <div className="card" role="alert" style={{ marginTop: 8 }}>
              <b className="neg">LIVE stays OFF: the server refused at {formatDate(refusal.at)}</b>
              <p className="muted small">{refusal.failed.length} check(s) failed. Fix them and switch ON again; nothing was changed.</p>
              <ul className="small">
                {refusal.failed.map((c) => (
                  <li key={c.code} style={{ marginBottom: 6 }}>
                    <b>{c.label}</b>: {c.detail}
                    {c.action && <div className="muted">What to do: {c.action}</div>}
                  </li>))}
              </ul>
            </div>)}
          {msg && <ErrorNotice error={msg} />}
          <details style={{ marginTop: 8 }} open={!d.arming_ok && !refusal}>
            <summary className="small">All safety checks ({d.failed.length} failing now)</summary>
            <div className="table-scroll" style={{ marginTop: 8 }}>
              <table className="data-table">
                <thead><tr><th>Check</th><th>Result</th><th>What the server found</th><th>What to do</th></tr></thead>
                <tbody>{d.checks.map((c) => (
                  <tr key={c.code}>
                    <td>{c.label}<span className="muted small"> ({c.kind === "arming" ? "to switch on" : "every order"})</span></td>
                    <td>{c.status === "PASS"
                      ? <span className="pill pill-ok"><CheckCircle2 size={12} aria-hidden /> pass</span>
                      : <span className="pill pill-danger"><XCircle size={12} aria-hidden /> fail</span>}</td>
                    <td className="small">{c.detail}</td>
                    <td className="muted small">{c.action}</td>
                  </tr>))}</tbody>
              </table>
            </div>
          </details>
          <details style={{ marginTop: 8 }}>
            <summary className="small">How this works</summary>
            <ul className="small">{d.how_it_works.map((h) => <li key={h}>{h}</li>)}</ul>
          </details>
        </>
      )}
    </Section>
  );
}
