"use client";

import ConfirmButton from "@/components/ConfirmDialog";
import MomentumPipelinePanel from "@/components/MomentumPipelinePanel";
import ScannerPipeline from "@/components/ScannerPipeline";
import { Empty, ErrorNotice, Loading, Pct, Section, Stat, StatePill } from "@/components/ui";
import { apiPost } from "@/lib/api";
import type { StrategyOut } from "@/lib/cc";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

interface Summary {
  mode?: string;
  group?: string;
  trades: number;
  win_rate: number | null;
  net_sol: number;
  profit_factor: number | null;
  max_drawdown_sol: number;
  fees_sol: number;
  fees_share_of_size: number | null;
  net_without_best_trade_sol: number | null;
  net_without_best_day_sol: number | null;
  sample: string;
}

interface Insight {
  window_start: string;
  decisions: { stage: string; decision: string; count: number }[];
  gate_decisions: Record<string, number>;
  results: Summary[];
  by_lifecycle: Summary[];
  by_entry_type: Summary[];
  recent: {
    decided_at: string;
    symbol: string | null;
    mint: string;
    stage: string;
    decision: string;
    traded: boolean;
    state: string | null;
    score: number | null;
    entry_type: string | null;
    version: string | null;
    reasons: string[];
    checks: { name: string; status: string; detail: string }[];
  }[];
  notes: string[];
  min_sample: number;
}

const CHECK_LABEL: Record<string, string> = {
  exhaustion: "Late-entry / exhaustion cap", decay: "Momentum decay (fewer buyers)", independent_buyers: "Independent buyers",
  anti_dump: "Anti-dump", liquidity: "Minimum liquidity", qualified_wallets: "Qualified wallets",
};
const STATE_CLASS: Record<string, string> = { QUALIFIED: "pill pill-ok", WAIT: "pill pill-warn", WATCH: "pill",
  REJECTED: "pill pill-danger", STALE: "pill pill-warn" };

const sol = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(4)} SOL`);

function SummaryTable({ rows, label }: { rows: Summary[]; label: string }) {
  if (!rows.length) return <Empty>No closed momentum trades in this group yet.</Empty>;
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>{label}</th><th>Mode</th><th>Trades</th><th>Win rate</th><th>Net</th><th>PF</th><th>Max DD</th>
            <th>Fees (share of size)</th><th>Net w/o best trade</th><th>Net w/o best day</th><th>Sample</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.mode}-${r.group ?? "all"}`}>
              <td>{r.group ?? "All"}</td>
              <td>{r.mode}</td>
              <td>{r.trades}</td>
              <td><Pct value={r.win_rate} /></td>
              <td className={r.net_sol >= 0 ? "pos" : "neg"}>{sol(r.net_sol)}</td>
              <td>{r.profit_factor === null ? "—" : r.profit_factor.toFixed(2)}</td>
              <td>{r.max_drawdown_sol.toFixed(4)}</td>
              <td>{r.fees_sol.toFixed(4)} (<Pct value={r.fees_share_of_size} />)</td>
              <td>{sol(r.net_without_best_trade_sol)}</td>
              <td>{sol(r.net_without_best_day_sol)}</td>
              <td className={r.sample === "ok" ? "" : "muted"}>{r.sample}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Momentum strategy panel: mode and what it does now, the v2 checks that
 * are on, decisions with their explanations, results by lifecycle and
 * entry type, scanner health, and the separate emergency stop. Everything
 * comes from recorded data; empty groups are shown as empty. */
export default function MomentumPanel({ s, onChange }: { s: StrategyOut; onChange: (s: StrategyOut) => void }) {
  const { data, error } = useApi<Insight>("/api/strategies/solana_momentum/insight", undefined, {
    reloadOn: ["strategy.updated", "trade.closed"], refreshMs: 30000,
  });
  const cfg = s.config as Record<string, unknown>;
  const enabled = new Set(s.momentum?.enabled_checks ?? []);
  const threshold: Record<string, string> = {
    exhaustion: `5 m return ≤ ${Number(cfg.mv2_max_return_5m ?? 0) * 100}%`,
    independent_buyers: `breadth ≥ ${cfg.mv2_min_breadth}, top-3 ≤ ${Number(cfg.mv2_max_top3_share ?? 0) * 100}%`,
    liquidity: `≥ ${cfg.mv2_min_liquidity_sol} SOL`, qualified_wallets: `≥ ${cfg.mv2_min_qualified_wallets} wallets`,
    decay: "price up on fewer buyers", anti_dump: "no distribution / creator sell / seller surge",
  };

  async function stop(close: boolean) {
    await apiPost("/api/strategies/solana_momentum/emergency-stop",
      { confirm: "STOP solana_momentum", close_positions: close, reason: close ? "emergency stop + close" : "emergency stop" });
    onChange({ ...s, mode: "OFF", effective_mode: "OFF" });
  }

  return (
    <>
      <Section title="Momentum strategy">
        <div className="stat-grid">
          <Stat label="Mode">{s.mode}</Stat>
          <Stat label="Execution now" hint={s.execution?.reason}>{s.execution?.effective_execution ?? "—"}</Stat>
          <Stat label="Real money authorized" hint="global mode LIVE and the server's live locks open">
            {s.execution ? (s.execution.live_authorized ? "YES" : "NO") : "—"}
          </Stat>
          <Stat label="Strategy version" hint="1 = the original v1 signal; 2:… = v1 plus the checks that are on">
            {s.momentum?.version ?? "—"}
          </Stat>
          <Stat label="Watching (OFF, or no free slot)">{s.momentum?.monitor_when_off ? "yes (stream only, never a trade)" : "no"}</Stat>
          <Stat label="Open positions">{s.open_positions ?? 0}</Stat>
        </div>
        <div className="table-wrap">
          <table className="data-table">
            <thead><tr><th>v2 check</th><th>Status</th><th>Threshold</th></tr></thead>
            <tbody>
              {Object.keys(CHECK_LABEL).map((c) => (
                <tr key={c}>
                  <td>{CHECK_LABEL[c]}</td>
                  <td>{enabled.has(c) ? <span className="pill pill-ok">ON (enforced)</span> : <span className="pill">off (recorded only)</span>}</td>
                  <td className="muted small">{threshold[c]}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="muted small">Switch checks on or off and edit thresholds in the configuration below. Each check is evaluated
          on every decision either way, so its effect can be compared before it is enforced.</p>
        <div className="btn-row">
          <ConfirmButton label="Emergency stop" title="Stop momentum entries now?" danger
            body={<p>Sets momentum OFF. Open positions stay open and keep their normal exit management.</p>}
            typeToConfirm="STOP" onConfirm={() => stop(false)} />
          <ConfirmButton label="Emergency stop and sell momentum positions" title="Stop and sell every momentum position?" danger
            body={<p>Sets momentum OFF and asks the position loop to sell each open momentum position ({s.open_positions ?? 0}) through
              the normal exit path. Other strategies are not touched.</p>}
            typeToConfirm="SELL MOMENTUM" onConfirm={() => stop(true)} />
        </div>
      </Section>

      <Section title="Momentum decisions and results">
        <ErrorNotice error={error} />
        {!data && !error && <Loading />}
        {data && (
          <>
            <div className="stat-grid">
              {data.decisions.length === 0 ? <Stat label="Decisions (24 h)">none</Stat> : data.decisions.map((d) => (
                <Stat key={`${d.stage}-${d.decision}`} label={`${d.stage} ${d.decision}`}>{d.count}</Stat>
              ))}
              {Object.entries(data.gate_decisions).map(([k, n]) => <Stat key={k} label={`gate ${k} (24 h)`}>{n}</Stat>)}
            </div>
            <h3 className="status-label">All closed trades</h3>
            <SummaryTable rows={data.results} label="Strategy" />
            <h3 className="status-label">By token lifecycle at entry</h3>
            <SummaryTable rows={data.by_lifecycle} label="Lifecycle" />
            <h3 className="status-label">By entry type</h3>
            <SummaryTable rows={data.by_entry_type} label="Entry type" />
            <h3 className="status-label">Recent decisions and why</h3>
            {data.recent.length === 0 ? <Empty>No momentum decision recorded yet.</Empty> : (
              <div className="table-wrap">
                <table className="data-table">
                  <thead><tr><th>When</th><th>Token</th><th>Stage</th><th>State</th><th>Score</th><th>Entry type</th><th>Why</th></tr></thead>
                  <tbody>
                    {data.recent.map((r) => (
                      <tr key={`${r.mint}-${r.decided_at}`}>
                        <td className="muted">{formatDate(r.decided_at)}</td>
                        <td>{r.symbol ?? r.mint.slice(0, 8)}</td>
                        <td>{r.stage}{r.traded ? " (traded)" : ""}</td>
                        <td>{r.state ? <span className={STATE_CLASS[r.state] ?? "pill"}>{r.state}</span> : <span className="muted">{r.decision}</span>}</td>
                        <td>{r.score === null ? "—" : r.score.toFixed(2)}</td>
                        <td className="muted">{r.entry_type ?? "—"}</td>
                        <td className="small">{r.reasons.join("; ") || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <ul className="muted small">{data.notes.map((n) => <li key={n}>{n}</li>)}</ul>
          </>
        )}
      </Section>

      <MomentumPipelinePanel />

      <Section title="Scanner and data sources">
        <ScannerPipeline compact />
        <p className="muted small">If the stream or a required source is stale, the gate refuses new entries with the reason; open
          positions keep being managed. <StatePill state="INFO" label="X is optional and never blocks the scanner" /></p>
      </Section>
    </>
  );
}
