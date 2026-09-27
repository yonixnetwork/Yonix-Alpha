"use client";

import { useEffect, useState } from "react";
import { ErrorNotice, Loading, Section } from "@/components/ui";
import { apiPut, ApiError } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import RuntimeApply from "@/components/RuntimeApply";

interface FuturesStatus {
  settings: Record<string, string>;
  limits: Record<string, [string, string]>;
  venues: Record<string, { ready: boolean; reason: string | null; report: { status: string; balance: string | null; quote: string | null; at: string } | null }>;
}

const LABELS: Record<string, string> = {
  max_leverage: "Max leverage (x)",
  min_free_balance: "Free balance kept in reserve (quote)",
  balance_max_age_seconds: "Balance sync max age (s)",
  max_fill_deviation_pct: "Max entry fill deviation from plan (%)",
};

/** Futures / FX live execution: per-venue readiness reported by
 *  services/execution-futures, and its runtime settings. */
export default function FuturesLive() {
  const st = useApi<FuturesStatus>("/api/live/futures", undefined, { refreshMs: 15000 });
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (st.data) setDraft(st.data.settings);
  }, [st.data]);

  async function save() {
    setError(null);
    setSaved(false);
    try {
      const out = await apiPut<Pick<FuturesStatus, "settings" | "limits">>("/api/live/futures/settings", draft);
      if (st.data) st.setData({ ...st.data, ...out });
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Save failed.");
    }
  }

  return (
    <Section title="Futures & FX (Binance · Bybit · Hyperliquid · MT5)" actions={saved ? <RuntimeApply inline /> : undefined}>
      <p className="muted">
        Meta Muse, Gold vs BTC, Confluence and the Hyperliquid grid trade live only when the environment locks are open, the global mode is LIVE, the
        strategy is AUTO or MANUAL, and the venue below is READY. Every live position gets an exchange-side stop; if the stop cannot be placed the position
        is closed. IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION.
      </p>
      <ErrorNotice error={st.error ?? error} />
      {!st.data ? (
        <Loading />
      ) : (
        <>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Venue</th>
                  <th>Ready</th>
                  <th>Worker report</th>
                  <th>Balance</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(st.data.venues).map(([v, r]) => (
                  <tr key={v}>
                    <td>{v}</td>
                    <td>
                      <span className={r.ready ? "pill pill-ok" : "pill pill-off"}>{r.ready ? "READY" : "NOT READY"}</span>
                    </td>
                    <td className="muted small">{r.ready ? r.report?.status : r.reason}</td>
                    <td>{r.report?.balance ? `${r.report.balance} ${r.report.quote ?? ""}` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="form-grid">
            {Object.keys(st.data.settings).map((k) => (
              <div className="form-row" key={k}>
                <label htmlFor={`fut-${k}`}>
                  {LABELS[k] ?? k} ({st.data!.limits[k]?.[0]}–{st.data!.limits[k]?.[1]})
                </label>
                <input id={`fut-${k}`} value={draft[k] ?? ""} inputMode="decimal" onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} />
              </div>
            ))}
            <div>
              <button className="btn btn-sm" onClick={save}>
                Save futures settings
              </button>
            </div>
          </div>
        </>
      )}
    </Section>
  );
}
