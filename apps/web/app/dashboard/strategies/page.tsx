"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiGet, apiPut, ApiError } from "@/lib/api";
import { formatDate } from "@/lib/format";
import type { ModesOut, PipelineOut } from "@/lib/types";

const STRATEGY_INFO: Record<string, { title: string; status: string; note: string }> = {
  solana_fresh: {
    title: "Solana fresh launches (pump.fun bonding curve)",
    status: "IMPLEMENTED — AWAITING LIVE VERIFICATION",
    note: "Entry signal is an unvalidated heuristic (no backtested edge). Paper only until verified on live data.",
  },
  solana_migration: {
    title: "Solana post-migration (PumpSwap via Jupiter)",
    status: "IMPLEMENTED — AWAITING LIVE VERIFICATION",
    note: "Flow data has no wallet identities (DexScreener), so every trade needs operator approval.",
  },
  binance_futures: {
    title: "Binance USDT-M futures",
    status: "EXECUTION LAYER ONLY",
    note: "Order/position plumbing exists; no strategy feeds the safety gate yet.",
  },
};

const GLOBAL_HELP: Record<string, string> = {
  PAPER: "Executable decisions are simulated in the paper engine.",
  MANUAL: "Every executable decision waits for operator approval, then re-runs all checks.",
  LIVE: "Real orders. Refused unless all three environment locks are open on the server.",
};

function age(seconds: number | null): string {
  if (seconds === null) return "never";
  if (seconds < 90) return `${seconds}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

export default function StrategyCenterPage() {
  const router = useRouter();
  const [modes, setModes] = useState<ModesOut | null>(null);
  const [pipe, setPipe] = useState<PipelineOut | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [m, p] = await Promise.all([apiGet<ModesOut>("/api/control/modes"), apiGet<PipelineOut>("/api/control/pipeline")]);
      setModes(m);
      setPipe(p);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return router.replace("/login");
      setError("Failed to load strategy state.");
    }
  }, [router]);

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  async function setMode(path: string, mode: string) {
    setError(null);
    try {
      setModes(await apiPut<ModesOut>(path, { mode }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Change failed.");
    }
  }

  const hbAge = pipe?.stream.heartbeat_age_seconds ?? null;
  const streamOk = hbAge !== null && hbAge <= 60;
  const counters = pipe?.stream.counters ?? {};

  return (
    <div>
      <div className="page-header">
        <div className="page-title">Strategy Center</div>
      </div>
      {error && <div className="error">{error}</div>}

      {modes && (
        <>
          <div className="card-grid">
            <div className="card">
              <div className="status-label">Global mode</div>
              <div className="btn-row">
                {(["PAPER", "MANUAL", "LIVE"] as const).map((m) => (
                  <button
                    key={m}
                    className={`btn btn-sm ${modes.global_mode === m ? "" : "btn-ghost"}`}
                    disabled={m === "LIVE" && !modes.env.live_permitted}
                    title={GLOBAL_HELP[m]}
                    onClick={() => setMode("/api/control/modes/global", m)}
                  >
                    {m}
                  </button>
                ))}
              </div>
              <div className="form-hint" style={{ marginTop: 8 }}>
                {GLOBAL_HELP[modes.global_mode]}
              </div>
            </div>
            <div className="card">
              <div className="status-label">Environment locks (server .env, read-only)</div>
              <dl className="kv">
                <dt>TRADING_ENABLED</dt>
                <dd>{String(modes.env.trading_enabled)}</dd>
                <dt>LIVE_TRADING_ENABLED</dt>
                <dd>{String(modes.env.live_trading_enabled)}</dd>
                <dt>PAPER_TRADING</dt>
                <dd>{String(modes.env.paper_trading)}</dd>
                <dt>Live possible</dt>
                <dd className={modes.env.live_permitted ? "level-CRITICAL" : "level-LOW"}>
                  {modes.env.live_permitted ? "YES" : "no"}
                </dd>
              </dl>
            </div>
          </div>

          <div className="section-title">Strategies</div>
          <table className="data-table">
            <thead>
              <tr>
                <th>Strategy</th>
                <th>Verification</th>
                <th>Mode</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(modes.strategies).map(([name, mode]) => (
                <tr key={name}>
                  <td>
                    <div>{STRATEGY_INFO[name]?.title ?? name}</div>
                    <div className="form-hint">{STRATEGY_INFO[name]?.note}</div>
                  </td>
                  <td>
                    <span className="pill pill-warn">{STRATEGY_INFO[name]?.status ?? "UNKNOWN"}</span>
                  </td>
                  <td>
                    <select value={mode} onChange={(e) => setMode(`/api/control/modes/strategy/${name}`, e.target.value)}>
                      <option value="OFF">OFF</option>
                      <option value="PAPER">PAPER</option>
                      <option value="MANUAL">MANUAL (approve each)</option>
                      <option value="AUTO">AUTO (bounded by global mode)</option>
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {pipe && (
        <>
          <div className="section-title">Solana pipeline health</div>
          {!streamOk && (
            <div className="notice notice-warn">
              {hbAge === null
                ? "The pump.fun stream has not delivered any event yet. Check SOLANA_WS_URL and the engine-solana-discovery logs, and run the live verification tool (docs/CONTROL_CENTER.md)."
                : `The pump.fun stream last delivered an event ${age(hbAge)}. Gate data is stale, so every evaluation will return NO_TRADE until it recovers.`}
            </div>
          )}
          <div className="card-grid">
            <div className="card">
              <div className="status-label">Stream</div>
              <dl className="kv">
                <dt>Last event</dt>
                <dd className={streamOk ? "level-LOW" : "level-CRITICAL"}>{age(hbAge)}</dd>
                <dt>Notifications</dt>
                <dd>{counters.notifications ?? 0}</dd>
                <dt>Creates / trades</dt>
                <dd>
                  {counters.create ?? 0} / {counters.trade ?? 0}
                </dd>
                <dt>Graduations / migrations</dt>
                <dd>
                  {counters.complete ?? 0} / {counters.migration ?? 0}
                </dd>
                <dt>Non-SOL skipped</dt>
                <dd>{counters.skipped_non_sol ?? 0}</dd>
                <dt>Mints tracked</dt>
                <dd>{pipe.stream.tracked_recent_mints}</dd>
              </dl>
            </div>
            <div className="card">
              <div className="status-label">Funnel (cumulative)</div>
              <dl className="kv">
                <dt>Considered</dt>
                <dd>{pipe.funnel.considered ?? 0}</dd>
                <dt>Failed prefilter</dt>
                <dd>{pipe.funnel.prefilter_failed ?? 0}</dd>
                <dt>Deferred (budget full)</dt>
                <dd>{pipe.funnel.budget_full ?? 0}</dd>
                <dt>Promoted</dt>
                <dd>{pipe.funnel.promoted ?? 0}</dd>
                <dt>Migration candidates</dt>
                <dd>{pipe.funnel.migrations ?? 0}</dd>
                <dt>Last run</dt>
                <dd>{pipe.funnel.last_run_at ? formatDate(pipe.funnel.last_run_at) : "never"}</dd>
              </dl>
            </div>
            <div className="card">
              <div className="status-label">Decisions (24 h)</div>
              <dl className="kv">
                {Object.keys(pipe.decisions_24h).length === 0 && (
                  <>
                    <dt>none</dt>
                    <dd>—</dd>
                  </>
                )}
                {Object.entries(pipe.decisions_24h).map(([d, n]) => (
                  <div key={d} style={{ display: "contents" }}>
                    <dt>{d}</dt>
                    <dd>{n}</dd>
                  </div>
                ))}
                <dt>Last assessment</dt>
                <dd>{pipe.last_assessment_at ? formatDate(pipe.last_assessment_at) : "never"}</dd>
              </dl>
            </div>
            <div className="card">
              <div className="status-label">Stream candidates by state</div>
              <dl className="kv">
                {Object.entries(pipe.candidates).map(([s, n]) => (
                  <div key={s} style={{ display: "contents" }}>
                    <dt>{s}</dt>
                    <dd>{n}</dd>
                  </div>
                ))}
                {Object.keys(pipe.candidates).length === 0 && (
                  <>
                    <dt>none yet</dt>
                    <dd>—</dd>
                  </>
                )}
              </dl>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
