"use client";

// Portfolio risk: limits that apply to every strategy and mode together,
// the live picture they are checked against, and the switches for optional
// heavy work (backtests, parameter optimisation).

import { useEffect, useState } from "react";
import { errText, Info, n } from "@/components/StrategyUi";
import { apiPut } from "@/lib/api";
import { useApi } from "@/lib/useApi";

type P = { limits: Record<string, string | boolean | number>; rules: Record<string, [string, string | null, string | null]>;
  state: { open_positions: number; open_risk_quote: string; total_exposure_quote: string; exposure_per_asset_quote: Record<string, string>;
    realized_today_quote: string; paper_equity_quote: string; paper_equity_peak: string; current_drawdown_pct: string;
    positions: Record<string, string>[] } };

const LABEL: Record<string, [string, string]> = {
  max_portfolio_risk_quote: ["Max total open risk (USDT)", "Sum of what every open position loses at its stop."],
  max_open_positions: ["Max open positions", "Across all strategies and modes."],
  max_total_exposure_quote: ["Max total exposure (USDT)", "Sum of open entry costs."],
  max_exposure_per_asset_quote: ["Max exposure per coin (USDT)", "BTC from three strategies counts as one BTC exposure."],
  max_daily_loss_quote: ["Max daily loss (USDT)", "Realized losses since 00:00 UTC; new entries stop when reached."],
  max_drawdown_pct: ["Drawdown shutdown (%)", "Paper equity below its high by this much sets the emergency stop. Open positions keep their protection."],
  backtests_enabled: ["Server backtests", "Always off: historical backtests run locally on your own computer, never on this server (docs/LOCAL_BACKTESTS.md)."],
  optimization_enabled: ["Parameter grid in backtests", "Only for the server queue, which is disabled; the local runner's own config decides this."],
};

// Server backtests are disabled by policy (backtest.policy); shown, not editable.
const READ_ONLY = new Set(["backtests_enabled", "optimization_enabled"]);

export default function Portfolio() {
  const { data, error, reload } = useApi<P>("/api/crypto/portfolio", undefined, { refreshMs: 15000 });
  const [form, setForm] = useState<Record<string, string | boolean>>({});
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    if (data) setForm(Object.fromEntries(Object.keys(LABEL).map((k) => [k, data.limits[k] as string | boolean])));
  }, [data]);
  async function save() {
    try {
      await apiPut("/api/crypto/portfolio", { expected_version: data!.limits.version, ...form });
      setMsg("Saved.");
      reload();
    } catch (e) {
      setMsg(errText(e));
    }
  }
  const st = data?.state;
  return (
    <div>
      <div className="page-header"><div className="page-title">Crypto Trading · Portfolio risk</div></div>
      <div className="notice">Checked before every new entry of every strategy (paper and exchange demo). Several strategies
        signalling at once cannot add up beyond these limits. Limits are never raised automatically.</div>
      {error && <div className="error">{error}</div>}
      {st && (
        <div className="stat-grid" style={{ marginTop: 12 }}>
          <div className="stat"><div className="stat-label">Open positions</div><div className="stat-value">{st.open_positions}</div></div>
          <div className="stat"><div className="stat-label">Open risk (USDT)</div><div className="stat-value">{n(st.open_risk_quote)}</div></div>
          <div className="stat"><div className="stat-label">Exposure (USDT)</div><div className="stat-value">{n(st.total_exposure_quote)}</div></div>
          <div className="stat"><div className="stat-label">Realized today</div><div className="stat-value">{n(st.realized_today_quote)}</div></div>
          <div className="stat"><div className="stat-label">Paper drawdown</div><div className="stat-value">{n(st.current_drawdown_pct)}%</div></div>
        </div>
      )}
      {st && Object.keys(st.exposure_per_asset_quote).length > 0 && (
        <div className="small" style={{ marginTop: 6 }}>Per coin: {Object.entries(st.exposure_per_asset_quote).map(([k, v]) => `${k} ${n(v)} USDT`).join(" · ")}</div>)}
      {data && (
        <div className="card" style={{ padding: 12, marginTop: 12 }}>
          <div className="form-grid">
            {Object.entries(LABEL).map(([k, [label, help]]) => (
              <div key={k} className="form-row"><label>{label}<Info text={help} /></label>
                {typeof data.limits[k] === "boolean" ? (
                  <select value={form[k] ? "1" : "0"} disabled={READ_ONLY.has(k)}
                    onChange={(e) => setForm({ ...form, [k]: e.target.value === "1" })}>
                    <option value="0">Off</option><option value="1">On</option></select>
                ) : <input value={String(form[k] ?? "")} inputMode="decimal" onChange={(e) => setForm({ ...form, [k]: e.target.value })} />}
              </div>
            ))}
          </div>
          <div className="btn-row" style={{ marginTop: 8 }}><button className="btn btn-sm" onClick={save}>Save limits</button>
            <span className="muted small">Version {String(data.limits.version)}</span>{msg && <span className="small">{msg}</span>}</div>
        </div>
      )}
      {st && st.positions.length > 0 && (
        <div className="table-scroll" style={{ marginTop: 12 }}><table className="data-table"><thead><tr><th>Mode</th><th>Strategy</th><th>Symbol</th><th>Qty</th><th>Entry</th><th>Stop</th><th>Risk</th><th>Exposure</th></tr></thead>
          <tbody>{st.positions.map((p, i) => (
            <tr key={i}><td>{p.mode}</td><td>{p.strategy_id}</td><td>{p.symbol}</td><td className="mono">{n(p.qty, 6)}</td><td className="mono">{n(p.entry, 4)}</td>
              <td className="mono">{n(p.stop, 4)}</td><td className="mono">{n(p.risk, 4)}</td><td className="mono">{n(p.exposure, 2)}</td></tr>))}</tbody></table></div>
      )}
    </div>
  );
}
