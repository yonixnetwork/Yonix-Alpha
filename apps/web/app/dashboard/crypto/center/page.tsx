"use client";

// Crypto Trading - Strategy Center: every registered strategy, its
// lifecycle status, mode and latest out-of-sample backtest figures, all from
// the backend. "backtest pending" means no backtest has run; nothing is
// filled in to make the table look complete.

import Link from "next/link";
import { useMemo, useState } from "react";
import { Info, ModePill, n, StatusPill, TierPill } from "@/components/StrategyUi";
import { useApi } from "@/lib/useApi";

type Row = {
  id: string; name: string; category: string; description: string; library: boolean; status: string; why: string;
  approved_for_consideration: boolean;
  paper: { open_positions?: number; open_cost_quote?: string; closed_trades?: number; realized_pnl_quote?: string; fees_quote?: string };
  enabled: boolean; mode: string; exchange: string | null; symbols: string[]; interval: string | null;
  risk: { per_trade: string; mode: string };
  backtest: null | { status: string; tier?: string; oos_trades?: number; oos_expectancy_r?: number | null;
    oos_profit_factor?: number | null; oos_max_drawdown_pct?: number | null; oos_net_return_pct?: number | null;
    oos_win_rate?: number | null; symbol: string; interval: string; finished_at: string; rank_key?: number[];
    oos_net_profit?: number | null; oos_fees?: number | null; oos_slippage?: number | null; data_source?: string | null;
    oos_period?: { from?: string; to?: string } | null;
    aggregate?: { finished: number; errored: number; pending: number; tiers: Record<string, number> } };
};
type Rejected = { id: string; name: string; status: string; reason: string };
type SortKey = "name" | "category" | "status" | "rank" | "expectancy" | "drawdown" | "trades";

// The figures are the WORST symbol of the strategy's latest batch, so one
// lucky symbol never makes a strategy look better than it is.
function aggText(r: Row): string {
  const b = r.backtest;
  if (!b) return "";
  const a = b.aggregate;
  if (!a || a.finished <= 1) return `${b.symbol} ${b.interval}`;
  const tiers = Object.entries(a.tiers).map(([k, v]) => `${v} ${k}`).join(", ");
  return `worst of ${a.finished}: ${b.symbol} ${b.interval}` + (tiers ? ` (${tiers})` : "") +
    (a.errored ? `, ${a.errored} failed` : "") + (a.pending ? `, ${a.pending} queued` : "");
}

function pct(x: number | null | undefined): string {
  return x == null ? "–" : `${(x * 100).toFixed(1)}%`;
}

// Paper figures straight from the stored simulated positions (no estimate).
function paperText(r: Row): string {
  const p = r.paper;
  if (!p || (!p.open_positions && !p.closed_trades)) return "";
  return `${p.open_positions ?? 0} open · ${p.closed_trades ?? 0} closed · realized ${n(p.realized_pnl_quote)} USDT`;
}

export default function StrategyCenter() {
  const { data, error } = useApi<{ strategies: Row[]; ranking_rule: string;
    paper_accounts: { exchange: string; quote: string; starting_balance: string; cash: string }[] }>("/api/crypto/center/strategies",
    undefined, { refreshMs: 30000 });
  const rejected = useApi<Rejected[]>("/api/crypto/center/rejected");
  const [q, setQ] = useState("");
  const [cat, setCat] = useState("");
  const [status, setStatus] = useState("");
  const [mode, setMode] = useState("");
  const [sort, setSort] = useState<SortKey>("rank");
  const [asc, setAsc] = useState(true);

  const rows = useMemo(() => {
    const list = (data?.strategies ?? []).filter((r) =>
      (!q || `${r.name} ${r.id} ${r.description}`.toLowerCase().includes(q.toLowerCase())) &&
      (!cat || r.category === cat) && (!status || r.status === status) && (!mode || r.mode === mode));
    const val = (r: Row): number | string => {
      const b = r.backtest;
      switch (sort) {
        case "name": return r.name;
        case "category": return r.category;
        case "status": return r.status;
        case "expectancy": return b?.oos_expectancy_r ?? -1e9;
        case "drawdown": return b?.oos_max_drawdown_pct ?? 1e9;
        case "trades": return b?.oos_trades ?? -1;
        default: return b?.rank_key ? b.rank_key[0] * 1e6 + b.rank_key[1] * 1e3 + b.rank_key[2] : 9e9;
      }
    };
    return [...list].sort((a, b) => {
      const x = val(a), y = val(b);
      const c = typeof x === "string" ? x.localeCompare(String(y)) : (x as number) - (y as number);
      return asc ? c : -c;
    });
  }, [data, q, cat, status, mode, sort, asc]);

  const cats = [...new Set((data?.strategies ?? []).map((r) => r.category))].sort();
  const statuses = [...new Set((data?.strategies ?? []).map((r) => r.status))].sort();
  const th = (k: SortKey, label: string, info?: string) => (
    <th className="th-sort" aria-sort={sort === k ? (asc ? "ascending" : "descending") : "none"}
      onClick={() => (sort === k ? setAsc(!asc) : (setSort(k), setAsc(k !== "expectancy" && k !== "trades")))}>
      {label}{sort === k ? (asc ? " ▲" : " ▼") : ""}{info && <Info k={info} />}
    </th>
  );

  return (
    <div>
      <div className="page-header"><div className="page-title">Crypto Trading · Strategy Center</div></div>
      <div className="notice">
        Every strategy runs only through the central risk and order layer, and starts disabled. Enabling one never
        authorizes live trading (live orders are locked). Figures are YonixAlpha&apos;s own out-of-sample backtests unless
        labeled otherwise; published research is cited on each strategy&apos;s page, not counted as evidence.
      </div>
      {data && <div className="muted small" style={{ marginTop: 6 }}>Ranking rule: {data.ranking_rule}.</div>}
      {data && data.paper_accounts.length > 0 && <div className="small" style={{ marginTop: 6 }}>
        Paper balances (simulated): {data.paper_accounts.map((a) => `${a.exchange} ${n(a.cash)} ${a.quote} cash (started ${n(a.starting_balance)})`).join(" · ")}</div>}
      <div className="muted small" style={{ marginTop: 4 }}>Backtest figures are the out-of-sample period of the worst symbol of
        the latest stored batch, after fees and slippage; nothing is shown until a result is stored.</div>
      {error && <div className="error">{error}</div>}
      <div className="strategy-filters" role="search">
        <input aria-label="Search strategies" placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} />
        <select aria-label="Category" value={cat} onChange={(e) => setCat(e.target.value)}>
          <option value="">All categories</option>{cats.map((c) => <option key={c}>{c}</option>)}
        </select>
        <select aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All statuses</option>{statuses.map((c) => <option key={c}>{c}</option>)}
        </select>
        <select aria-label="Mode" value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="">All modes</option><option value="DISABLED">Disabled</option>
          <option value="DEMO">Paper</option><option value="EXCHANGE_DEMO">Exchange demo</option>
        </select>
        <span className="muted small" style={{ alignSelf: "center" }}>{rows.length} of {data?.strategies.length ?? 0}</span>
      </div>
      {data && (
        <div className="mobile-only" style={{ width: "100%" }}><div style={{ display: "grid", gap: 8 }}>
          {rows.map((r) => (
            <Link key={r.id} href={`/dashboard/crypto/center/${r.id}`} className="sc-card" style={{ display: "block", color: "inherit", textDecoration: "none" }}>
              <div><strong>{r.name}</strong></div>
              <div className="muted small">{r.category}{r.interval ? ` · ${r.interval}` : ""}</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 6 }}>
                <StatusPill status={r.status} /><ModePill mode={r.mode} enabled={r.enabled} /><TierPill tier={r.backtest?.tier} />
              </div>
              {paperText(r) && <div className="small" style={{ marginTop: 6 }}>{paperText(r)}</div>}
              {r.backtest && <div className="small mono" style={{ marginTop: 6 }}>
                OOS {n(r.backtest.oos_trades, 0)} trades · PnL {n(r.backtest.oos_net_profit)} · win {pct(r.backtest.oos_win_rate)} · DD {n(r.backtest.oos_max_drawdown_pct)}%
                <div className="muted">{aggText(r)}</div></div>}
            </Link>
          ))}
        </div></div>
      )}
      {data && (
        <div className="table-scroll desktop-only">
          <table className="data-table">
            <thead><tr>
              {th("name", "Strategy")}{th("category", "Category")}{th("status", "Status")}<th>Mode / risk</th>
              {th("rank", "Verdict", "tier")}{th("trades", "OOS trades", "trades")}{th("expectancy", "OOS expectancy (R)", "expectancy_r")}
              <th>OOS net PnL</th><th>Win rate</th>
              <th>OOS PF<Info k="profit_factor" /></th>{th("drawdown", "OOS max DD %", "max_drawdown_pct")}<th>Fees + slippage</th>
              <th>Period · data</th><th>Paper (simulated)</th>
            </tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td><Link href={`/dashboard/crypto/center/${r.id}`}>{r.name}</Link>
                    <div className="muted small">{r.id}{r.interval ? ` · ${r.interval}` : ""}{r.symbols.length ? ` · ${r.symbols.join(", ")}` : ""}</div></td>
                  <td className="small">{r.category}</td>
                  <td><StatusPill status={r.status} /><div className="muted small">{r.why}</div></td>
                  <td><ModePill mode={r.mode} enabled={r.enabled} /><div className="muted small">risk {r.risk.per_trade}</div></td>
                  <td><TierPill tier={r.backtest?.tier} />
                    {r.backtest && <div className="muted small">{aggText(r)}</div>}</td>
                  <td className="mono">{n(r.backtest?.oos_trades, 0)}</td>
                  <td className="mono">{n(r.backtest?.oos_expectancy_r, 3)}</td>
                  <td className="mono">{n(r.backtest?.oos_net_profit, 2)}</td>
                  <td className="mono">{pct(r.backtest?.oos_win_rate)}</td>
                  <td className="mono">{n(r.backtest?.oos_profit_factor, 2)}</td>
                  <td className="mono">{n(r.backtest?.oos_max_drawdown_pct, 2)}</td>
                  <td className="mono">{r.backtest && r.backtest.oos_fees != null ? n((r.backtest.oos_fees ?? 0) + (r.backtest.oos_slippage ?? 0), 2) : "–"}</td>
                  <td className="small">{r.backtest?.oos_period ? `${(r.backtest.oos_period.from ?? "").slice(0, 10)} to ${(r.backtest.oos_period.to ?? "").slice(0, 10)}` : "–"}
                    {r.backtest?.data_source && <div className="muted">{r.backtest.data_source}</div>}</td>
                  <td className="small">{paperText(r) || "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {rejected.data && (
        <details style={{ marginTop: 16 }}>
          <summary className="section-title">Candidates not in the library ({rejected.data.length})</summary>
          <table className="data-table">
            <thead><tr><th>Candidate</th><th>Status</th><th>Reason</th></tr></thead>
            <tbody>{rejected.data.map((r) => (
              <tr key={r.id}><td>{r.name}</td><td><span className="pill pill-off">{r.status}</span></td><td className="small">{r.reason}</td></tr>
            ))}</tbody>
          </table>
        </details>
      )}
    </div>
  );
}
