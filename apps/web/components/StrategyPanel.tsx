"use client";

import { useEffect, useState } from "react";
import ConfirmButton from "@/components/ConfirmDialog";
import { ErrorNotice, modeClass, Money, Pct, Stat } from "@/components/ui";
import { apiPut } from "@/lib/api";
import type { StrategyOut } from "@/lib/cc";
import { formatDate } from "@/lib/format";
import RuntimeApply from "@/components/RuntimeApply";

const MODES = ["OFF", "MANUAL", "PAPER", "AUTO"];
const MODE_HELP: Record<string, string> = {
  OFF: "No evaluation, no entries.",
  MANUAL: "Every entry waits for your approval; approval never bypasses a safety check.",
  PAPER: "Qualified trades are simulated in the paper book automatically.",
  AUTO: "Automatic within the gate, never waits for approval. Paper unless the global mode is LIVE, the server's environment locks are open and the live worker is ready (Pump.fun strategies only).",
};

function asText(v: unknown): string {
  if (v === null || v === undefined) return "";
  return typeof v === "object" ? JSON.stringify(v) : String(v);
}

function parse(v: string, original: unknown): unknown {
  if (typeof original === "number") return v.trim() === "" ? v : Number(v);
  if (typeof original === "boolean") return v === "true";
  if (Array.isArray(original)) {
    try {
      return JSON.parse(v);
    } catch {
      return v;
    }
  }
  if (original === null && v === "") return null;
  return v;
}

/** Mode control (with confirmation), status, results and the validated
 * config editor for one strategy or venue. */
export default function StrategyPanel({ s, onChange }: { s: StrategyOut; onChange: (s: StrategyOut) => void }) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [modeSaved, setModeSaved] = useState(false);

  useEffect(() => {
    setDraft(Object.fromEntries(s.editable.map((k) => [k, asText(s.config[k])])));
  }, [s]);

  async function setMode(mode: string) {
    setModeSaved(false);
    onChange(await apiPut<StrategyOut>(`/api/strategies/${s.name}/mode`, { mode }));
    setModeSaved(true);
  }

  async function save() {
    setError(null);
    setSaved(false);
    const changed: Record<string, unknown> = {};
    for (const k of s.editable) {
      if (draft[k] !== asText(s.config[k])) changed[k] = parse(draft[k], s.config[k]);
    }
    try {
      onChange(await apiPut<StrategyOut>(`/api/strategies/${s.name}/config`, { config: changed }));
      setSaved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed.");
    }
  }

  return (
    <div>
      <div className="notice">{s.status}</div>
      {s.mode && (
        <div className="card">
          <div className="status-label">Mode</div>
          <div className="btn-row" role="group" aria-label={`${s.label} mode`}>
            {MODES.map((m) => (
              <ConfirmButton
                key={m}
                label={m}
                className={s.mode === m ? "btn btn-sm" : "btn btn-ghost btn-sm"}
                disabled={s.mode === m}
                ariaLabel={`Set ${s.label} mode to ${m}`}
                title={`Set ${s.label} to ${m}?`}
                body={MODE_HELP[m]}
                danger={m === "AUTO"}
                onConfirm={() => setMode(m)}
              />
            ))}
          </div>
          <div className="muted" style={{ marginTop: 8 }}>
            {modeSaved && <div><RuntimeApply inline /></div>}
            Stored <span className={modeClass(s.mode)}>{s.mode}</span> · effective{" "}
            <span className={modeClass(s.effective_mode)}>{s.effective_mode}</span>
            {s.venue_mode_key && s.venue_mode_key !== s.name && <> (most restrictive of this and venue {s.venue_mode_key})</>}
          </div>
        </div>
      )}
      {s.trades !== undefined && (
        <div className="stat-grid">
          <Stat label="Open positions">{s.open_positions ?? 0}</Stat>
          <Stat label="Closed trades">{s.trades}</Stat>
          <Stat label="Win rate">
            <Pct value={s.win_rate} />
          </Stat>
          <Stat label="Realized PnL">
            <Money value={s.total_pnl} currency={s.currency} />
          </Stat>
          <Stat label="Last decision">{formatDate(s.last_decision_at ?? null)}</Stat>
        </div>
      )}
      {s.editable.length > 0 && (
        <div className="card">
          <div className="status-label">Configuration (validated on save; secrets never live here)</div>
          <div className="form-grid">
            {s.editable.map((k) => (
              <div className="form-row" key={k}>
                <label htmlFor={`cfg-${s.name}-${k}`}>
                  {k.replace(/_/g, " ")}
                  {k.startsWith("manual_") && (
                    <span className="muted">{k.endsWith("_sol") ? " (SOL; empty = automatic)" : " (fraction, 0.2 = 20%; empty = automatic)"}</span>
                  )}
                </label>
                {typeof s.config[k] === "boolean" ? (
                  <select id={`cfg-${s.name}-${k}`} value={draft[k] ?? ""} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })}>
                    <option value="true">true</option>
                    <option value="false">false</option>
                  </select>
                ) : (
                  <input id={`cfg-${s.name}-${k}`} value={draft[k] ?? ""} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />
                )}
              </div>
            ))}
          </div>
          <ErrorNotice error={error} />
          {saved && <div><RuntimeApply inline /></div>}
          <div className="btn-row" style={{ marginTop: 12 }}>
            <button className="btn btn-sm" onClick={save}>
              Save configuration
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
