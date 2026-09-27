"use client";

import { Fragment, useState } from "react";
import { Eye } from "lucide-react";
import ExecutionFunnel, { TokenPipeline } from "@/components/ExecutionFunnel";
import { Empty, ErrorNotice, PageHeader, Section, Stat, TokenLink } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { BuyButton } from "@/components/ManualTrade";

type Metrics = Record<string, string | number | boolean | null>;
type Row = { mint: string; symbol: string | null; outcome: string; trend: string | null; reasons: string[]; decided_at: string;
  candidate_id: string | null; metrics: Metrics };
type LiveRow = { mint: string; symbol: string | null; state: string; age_seconds: number | null; trend: string | null;
  reasons: string[]; metrics: Metrics };
type Detail = {
  mint: string; symbol: string | null;
  observation: { outcome: string | null; decided_at: string | null; report: Record<string, unknown> | null };
  candidates: { id: string; engine: string; state: string; state_history: { state: string; at: string; reason?: string }[];
    latest_assessment: { decision: string; status: string; reasons: string[] | null;
      findings: { code: string; level: string; message: string; action: string }[] | null } | null }[];
};

const OUTCOMES = ["", "PROMOTE", "REJECT", "NO_TRADE", "MIGRATION_DETECTED"];

function outcomeClass(o: string | null | undefined): string {
  if (o === "PROMOTE" || o === "MIGRATION_DETECTED") return "pill pill-ok";
  if (o === "CONTINUE_MONITORING" || o === "FRESH_OBSERVING" || o === "OBSERVING") return "pill pill-warn";
  if (o === "REJECT" || o === "NO_TRADE") return "pill pill-danger";
  return "pill pill-off";
}

function M({ m, k }: { m: Metrics; k: string }) {
  const v = m?.[k];
  return <>{v === null || v === undefined ? "—" : String(v)}</>;
}

function Explain({ mint }: { mint: string }) {
  const { data, error } = useApi<Detail>(`/api/observations/${encodeURIComponent(mint)}`);
  if (error) return <ErrorNotice error={error} />;
  if (!data) return null;
  const r = (data.observation.report ?? {}) as { checkpoints?: Record<string, unknown>[]; halves?: Record<string, unknown>[];
    positive?: string[]; negative?: string[]; reasons?: string[]; metrics?: Metrics; trend?: string };
  return (
    <div className="card" aria-live="polite">
      <TokenPipeline mint={mint} />
      <div className="status-label">
        Observation: <span className={outcomeClass(data.observation.outcome)}>{data.observation.outcome ?? "—"}</span>{" "}
        {r.trend && <>trend <b>{r.trend}</b></>}
      </div>
      <ul className="reason-list">{(r.reasons ?? []).map((x, i) => <li key={i}>{x}</li>)}</ul>
      {r.checkpoints && r.checkpoints.length > 0 && (
        <table className="data-table">
          <thead><tr><th>Checkpoint</th><th>Trades</th><th>Buyers</th><th>Sellers</th><th>Volume (SOL)</th></tr></thead>
          <tbody>{r.checkpoints.map((c, i) => (
            <tr key={i}><td>{String(c.label)}</td><td>{String(c.trades)}</td><td>{String(c.unique_buyers)}</td>
              <td>{String(c.unique_sellers)}</td><td>{String(c.volume_sol)}</td></tr>))}</tbody>
        </table>
      )}
      {r.halves && r.halves.length === 2 && (
        <table className="data-table">
          <thead><tr><th>Half-window</th><th>Trades</th><th>New buyers</th><th>Sellers</th><th>Buy SOL</th><th>Sell SOL</th><th>Price</th></tr></thead>
          <tbody>{r.halves.map((h, i) => (
            <tr key={i}><td>{i === 0 ? "first" : "second"}</td><td>{String(h.trades)}</td><td>{String(h.new_buyers)}</td>
              <td>{String(h.unique_sellers)}</td><td>{String(h.buy_volume_sol)}</td><td>{String(h.sell_volume_sol)}</td>
              <td>{h.price_change_pct === null || h.price_change_pct === undefined ? "—" : `${h.price_change_pct}%`}</td></tr>))}</tbody>
        </table>
      )}
      {(r.positive?.length || r.negative?.length) ? (
        <div className="stat-grid">
          <Stat label="Positive signals">{(r.positive ?? []).join("; ") || "—"}</Stat>
          <Stat label="Negative signals">{(r.negative ?? []).join("; ") || "—"}</Stat>
        </div>
      ) : null}
      {r.metrics && (
        <div className="stat-grid">
          {Object.entries(r.metrics).map(([k, v]) => <Stat key={k} label={k.replace(/_/g, " ")}>{v === null ? "—" : String(v)}</Stat>)}
        </div>
      )}
      {data.candidates.map((c) => (
        <div key={c.id} className="notice">
          <b>{c.engine}</b> candidate — state <b>{c.state}</b>
          {c.latest_assessment && (
            <>
              {" "}· gate decision <b>{c.latest_assessment.decision}</b> ({c.latest_assessment.status})
              <ul className="reason-list">
                {(c.latest_assessment.findings ?? []).filter((f) => f.action !== "EXECUTE" || f.level !== "LOW").map((f, i) => (
                  <li key={i}><code>{f.code}</code> [{f.level} → {f.action}] {f.message}</li>
                ))}
              </ul>
            </>
          )}
          {c.state_history?.length ? <div className="muted">last: {c.state_history[c.state_history.length - 1].reason}</div> : null}
        </div>
      ))}
    </div>
  );
}

export default function ObservingPage() {
  const [outcome, setOutcome] = useState("");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const live = useApi<LiveRow[]>("/api/observations/live", undefined, { refreshMs: 5000 });
  const list = useApi<{ total: number; items: Row[] }>("/api/observations", { outcome: outcome || undefined, q: q || undefined, limit: 100 },
    { refreshMs: 15000, reloadOn: ["token.discovered"] });
  const stats = useApi<{ funnel: Record<string, string>; retained_outcomes: Record<string, number>; live: number }>(
    "/api/observations/stats", undefined, { refreshMs: 15000 });
  return (
    <div>
      <PageHeader title="Fresh Token Observation" icon={<Eye size={20} aria-hidden />}
        subtitle="Every new pump.fun token is observed before any decision. Activity (what the stream saw) is not a signal: a buy needs a BUY signal, risk approval and execution — each shown separately below." />
      {stats.data && (
        <div className="stat-grid">
          <Stat label="observing now">{stats.data.live}</Stat>
          {Object.entries(stats.data.retained_outcomes).map(([k, v]) => <Stat key={k} label={k}>{v}</Stat>)}
          {["promoted", "rejected", "expired", "migration_detected", "budget_full"].map((k) => (
            <Stat key={k} label={`funnel ${k.replace(/_/g, " ")}`}>{stats.data!.funnel[k] ?? "0"}</Stat>
          ))}
        </div>
      )}
      <ExecutionFunnel />
      <Section title="Under observation">
        <ErrorNotice error={live.error} />
        {live.data && live.data.length === 0 && <Empty>No token is inside its observation window right now.</Empty>}
        {live.data && live.data.length > 0 && (
          <table className="data-table">
            <thead><tr><th>Token</th><th>State</th><th>Age</th><th>Trend</th><th>Trades</th><th>Buyers</th><th>Liquidity state</th><th>Why</th><th>Action</th></tr></thead>
            <tbody>{live.data.map((r) => (
              <tr key={r.mint}>
                <td><TokenLink mint={r.mint} label={r.symbol} /></td>
                <td><span className={outcomeClass(r.state)}>{r.state}</span></td>
                <td>{r.age_seconds === null ? "—" : `${Math.round(r.age_seconds)}s`}</td>
                <td>{r.trend ?? "—"}</td><td><M m={r.metrics} k="trades_total" /></td><td><M m={r.metrics} k="unique_buyers_total" /></td>
                <td><M m={r.metrics} k="liquidity_state" /></td><td className="muted">{r.reasons?.[r.reasons.length - 1]}</td>
                <td><BuyButton mint={r.mint} engine="solana_fresh" source="observation" /></td>
              </tr>))}</tbody>
          </table>
        )}
      </Section>
      <Section title="Why didn't the bot trade this token?"
        actions={
          <div className="btn-row">
            <label className="sr-only" htmlFor="obs-outcome">Outcome</label>
            <select id="obs-outcome" value={outcome} onChange={(e) => setOutcome(e.target.value)}>
              {OUTCOMES.map((o) => <option key={o} value={o}>{o || "all outcomes"}</option>)}
            </select>
            <label className="sr-only" htmlFor="obs-q">Search</label>
            <input id="obs-q" placeholder="mint / symbol / name" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
        }>
        <ErrorNotice error={list.error} />
        {list.data && list.data.items.length === 0 && <Empty>No decided observations yet (kept for 3 days).</Empty>}
        {list.data && list.data.items.length > 0 && (
          <table className="data-table">
            <thead><tr><th>Token</th><th>Outcome</th><th>Trend</th><th>Trades</th><th>Buyers</th><th>Sellers</th><th>Volume</th>
              <th>Curve</th><th>Reason</th><th>Decided</th><th>Action</th></tr></thead>
            <tbody>{list.data.items.map((r) => (
              <Fragment key={r.mint}>
                <tr onClick={() => setOpen(open === r.mint ? null : r.mint)} className="clickable">
                  <td><TokenLink mint={r.mint} label={r.symbol} /></td>
                  <td><span className={outcomeClass(r.outcome)}>{r.outcome}</span></td>
                  <td>{r.trend ?? "—"}</td><td><M m={r.metrics} k="trades_total" /></td><td><M m={r.metrics} k="unique_buyers_total" /></td>
                  <td><M m={r.metrics} k="unique_sellers_total" /></td><td><M m={r.metrics} k="volume_total_sol" /></td>
                  <td><M m={r.metrics} k="liquidity_state" /></td>
                  <td className="muted">{r.reasons[r.reasons.length - 1]}</td><td>{formatDate(r.decided_at)}</td>
                  <td onClick={(e) => e.stopPropagation()}><BuyButton mint={r.mint} engine="solana_fresh" source="observation" /></td>
                </tr>
                {open === r.mint && (
                  <tr><td colSpan={11}><Explain mint={r.mint} /></td></tr>
                )}
              </Fragment>
            ))}</tbody>
          </table>
        )}
      </Section>
    </div>
  );
}
