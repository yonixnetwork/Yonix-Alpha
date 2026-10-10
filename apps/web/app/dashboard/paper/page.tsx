"use client";

import { useEffect, useState } from "react";
import { Wallet } from "lucide-react";
import ConfirmButton from "@/components/ConfirmDialog";
import PerformancePanel from "@/components/PerformancePanel";
import PositionsTable from "@/components/PositionsTable";
import { ErrorNotice, Money, PageHeader, Pct, Section, Stat } from "@/components/ui";
import { apiPost, apiPut, ApiError } from "@/lib/api";
import { formatDate, formatDecimal } from "@/lib/format";
import type { PaperAccountOut } from "@/lib/types";
import { useApi } from "@/lib/useApi";
import RuntimeApply from "@/components/RuntimeApply";

function AccountCard({ a, onReset }: { a: PaperAccountOut; onReset: () => void }) {
  const [balance, setBalance] = useState(a.starting_balance.replace(/\.?0+$/, ""));
  const winRate = a.closed_positions ? a.winning_positions / a.closed_positions : null;
  return (
    <div className="card">
      <div className="status-label">
        {a.name} · {a.quote_currency}
      </div>
      <dl className="kv">
        <dt>Equity</dt>
        <dd>
          {formatDecimal(a.equity, 6)} {a.quote_currency}
        </dd>
        <dt>Cash (available)</dt>
        <dd>{formatDecimal(a.cash_balance, 6)}</dd>
        <dt>Open positions</dt>
        <dd>
          {a.open_positions} ({formatDecimal(a.open_exposure, 6)} marked)
        </dd>
        <dt>Closed since reset</dt>
        <dd>
          {a.closed_positions} · win rate <Pct value={winRate} digits={0} />
        </dd>
        <dt>Realized PnL</dt>
        <dd>
          <Money value={a.realized_pnl} currency={a.quote_currency} digits={6} />
        </dd>
        <dt>Fees paid</dt>
        <dd>{formatDecimal(a.fees_paid, 6)}</dd>
        <dt>Since</dt>
        <dd>{formatDate(a.reset_at)}</dd>
      </dl>
      <div className="inline-form" style={{ marginTop: 10, marginBottom: 0 }}>
        <label htmlFor={`bal-${a.name}`} className="sr-only">
          New starting balance for {a.name}
        </label>
        <input id={`bal-${a.name}`} style={{ width: 110 }} value={balance} onChange={(e) => setBalance(e.target.value)} />
        <ConfirmButton
          label="Reset…"
          disabled={a.open_positions > 0}
          danger
          title={`Reset the ${a.name} paper book?`}
          body={
            <>
              Starts a new record at {balance} {a.quote_currency}. Past trades stay in the database but no longer count
              towards this book&apos;s statistics. Only possible with no open positions.
            </>
          }
          typeToConfirm={a.name}
          confirmLabel="Reset book"
          onConfirm={async () => {
            await apiPost(`/api/control/paper/accounts/${a.name}/reset`, { starting_balance: balance, confirm: a.name });
            onReset();
          }}
        />
      </div>
      {a.open_positions > 0 && <div className="form-hint">Reset is available only with no open positions.</div>}
    </div>
  );
}

interface PaperExecution {
  settings: { entry_failure_pct: string; exit_failure_pct: string; use_measured_live_rates: boolean; charge_live_fixed_costs: boolean;
    simulate_entry_delay?: boolean };
  drift?: { buy_pct: string | null; sell_pct: string | null; buy_n: number; sell_n: number; source: string | null };
  measured: Record<string, { orders: number; failed: number; failure_pct: string | null; usable: boolean }>;
  entry_pct: string;
  exit_pct: string;
  entry_source: string;
  exit_source: string;
}

function ExecutionFailures() {
  const { data, error, setData } = useApi<PaperExecution>("/api/paper/execution-settings");
  const [draft, setDraft] = useState<{ entry: string; exit: string; measured: boolean; fixed: boolean; delay: boolean } | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    if (data) setDraft({ entry: data.settings.entry_failure_pct, exit: data.settings.exit_failure_pct, measured: data.settings.use_measured_live_rates,
      fixed: data.settings.charge_live_fixed_costs, delay: data.settings.simulate_entry_delay !== false });
  }, [data]);
  if (error) return <ErrorNotice error={error} />;
  if (!data || !draft) return null;
  async function save() {
    setSaveError(null);
    setSaved(false);
    try {
      setData(
        await apiPut<PaperExecution>("/api/paper/execution-settings", {
          entry_failure_pct: draft!.entry,
          exit_failure_pct: draft!.exit,
          use_measured_live_rates: draft!.measured,
          charge_live_fixed_costs: draft!.fixed,
          simulate_entry_delay: draft!.delay,
        }),
      );
      setSaved(true);
    } catch (err) {
      setSaveError(err instanceof ApiError ? err.message : "Save failed.");
    }
  }
  return (
    <div className="card">
      <p className="muted">
        A share of paper entries and exits fail the way live transactions do (dropped, expired or rejected). A failed entry
        opens nothing; a failed exit is retried on the next tick at that tick&apos;s price. Rates are never invented: they are
        your setting (default 0%) until at least 20 live orders of a side have a final outcome, after which the measured
        live rate is used if enabled.
      </p>
      <div className="stat-grid">
        <Stat label="Entry failure rate in use">
          {data.entry_pct}% <span className="muted">({data.entry_source})</span>
        </Stat>
        <Stat label="Exit failure rate in use">
          {data.exit_pct}% <span className="muted">({data.exit_source})</span>
        </Stat>
        {data.drift && (
          <Stat label="Measured LIVE price drift charged to paper (buy / sell)">
            {data.drift.buy_pct ?? "none"}% / {data.drift.sell_pct ?? "none"}%{" "}
            <span className="muted">({data.drift.source ?? "-"})</span>
          </Stat>
        )}
        {Object.entries(data.measured).map(([side, m]) => (
          <Stat key={side} label={`Live ${side} orders measured`}>
            {m.orders} {m.failure_pct !== null ? <span className="muted">· {m.failure_pct}% failed</span> : null}
          </Stat>
        ))}
      </div>
      <div className="form-grid" style={{ marginTop: 12 }}>
        <div className="form-row">
          <label htmlFor="pe-entry">Entry failure % (0–50)</label>
          <input id="pe-entry" inputMode="decimal" value={draft.entry} onChange={(e) => setDraft({ ...draft, entry: e.target.value })} />
        </div>
        <div className="form-row">
          <label htmlFor="pe-exit">Exit failure % (0–50)</label>
          <input id="pe-exit" inputMode="decimal" value={draft.exit} onChange={(e) => setDraft({ ...draft, exit: e.target.value })} />
        </div>
        <div className="form-row">
          <label htmlFor="pe-measured">Use measured live rates when available</label>
          <select id="pe-measured" value={String(draft.measured)} onChange={(e) => setDraft({ ...draft, measured: e.target.value === "true" })}>
            <option value="true">yes</option>
            <option value="false">no</option>
          </select>
        </div>
        <div className="form-row">
          <label htmlFor="pe-fixed">Charge Solana paper trades the fixed costs a LIVE trade pays</label>
          <select id="pe-fixed" value={String(draft.fixed)} onChange={(e) => setDraft({ ...draft, fixed: e.target.value === "true" })}>
            <option value="true">yes (recommended: paper sizes, refuses and books as LIVE would)</option>
            <option value="false">no (paper ignores network / priority fees and rent)</option>
          </select>
          <div className="form-hint">The buy and sell network + priority fees and the rent reclaim fee, from the Live settings. Counted in the
            risk plan (smaller size, FIXED_COSTS_EXCEED_RISK) and charged to the paper book.</div>
        </div>
        <div className="form-row">
          <label htmlFor="pe-delay">Paper buys on the pump curve land after the measured LIVE delay</label>
          <select id="pe-delay" value={String(draft.delay)} onChange={(e) => setDraft({ ...draft, delay: e.target.value === "true" })}>
            <option value="true">yes (recommended: filled at the stream price when a LIVE buy would land)</option>
            <option value="false">no (filled at the decision price, plus the median LIVE drift when measured)</option>
          </select>
          <div className="form-hint">The delay is the median decision-to-confirmation time of the last confirmed LIVE buys (3 s until 5 exist).
            Until then the position is not managed; the fill moves with the market both ways.</div>
        </div>
      </div>
      <ErrorNotice error={saveError} />
      {saved && <div><RuntimeApply inline /></div>}
      <div className="btn-row" style={{ marginTop: 12 }}>
        <button className="btn btn-sm" onClick={save}>
          Save
        </button>
      </div>
    </div>
  );
}

type ExitProtection = { mode: string; min_sale_fee_multiple: string; min_remainder_fee_multiple: string; modes: string[] };

function ExitProtectionSettings() {
  const { data, error, setData } = useApi<ExitProtection>("/api/paper/exit-protection");
  const [draft, setDraft] = useState<ExitProtection | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => { if (data) setDraft(data); }, [data]);
  if (error) return <ErrorNotice error={error} />;
  if (!data || !draft) return null;
  async function save() {
    setSaveError(null);
    setSaved(false);
    try {
      setData(await apiPut<ExitProtection>("/api/paper/exit-protection", {
        mode: draft!.mode, min_sale_fee_multiple: draft!.min_sale_fee_multiple,
        min_remainder_fee_multiple: draft!.min_remainder_fee_multiple,
      }));
      setSaved(true);
    } catch (err) {
      setSaveError(err instanceof ApiError ? err.message : "Save failed.");
    }
  }
  return (
    <div className="card">
      <p className="muted">
        Partial take-profits never leave a remainder too small to sell: a leftover of dust or worth less than a few sell fees
        is sold together with the take-profit, and a take-profit worth less than a few sell fees waits for the next exit.
        Stop loss, trailing stop and manual / emergency exits are never changed. In PAPER mode LIVE exits are only checked
        and the would-be change is recorded on the position.
      </p>
      <div className="form-grid">
        <div className="form-row">
          <label htmlFor="xp-mode">Mode</label>
          <select id="xp-mode" value={draft.mode} onChange={(e) => setDraft({ ...draft, mode: e.target.value })}>
            {data.modes.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </div>
        <div className="form-row">
          <label htmlFor="xp-sale">A partial sale must return at least N sell fees</label>
          <input id="xp-sale" inputMode="decimal" value={draft.min_sale_fee_multiple}
            onChange={(e) => setDraft({ ...draft, min_sale_fee_multiple: e.target.value })} />
        </div>
        <div className="form-row">
          <label htmlFor="xp-rem">A remainder must be worth at least N sell fees</label>
          <input id="xp-rem" inputMode="decimal" value={draft.min_remainder_fee_multiple}
            onChange={(e) => setDraft({ ...draft, min_remainder_fee_multiple: e.target.value })} />
        </div>
      </div>
      <ErrorNotice error={saveError} />
      {saved && <div><RuntimeApply inline /></div>}
      <div className="btn-row" style={{ marginTop: 12 }}>
        <button className="btn btn-sm" onClick={save}>Save</button>
      </div>
    </div>
  );
}

export default function PaperTradingPage() {
  const { data: accounts, error, reload } = useApi<PaperAccountOut[]>("/api/control/paper/accounts", undefined, {
    reloadOn: ["balance.updated", "trade.closed"],
    refreshMs: 30000,
  });
  return (
    <div>
      <PageHeader
        title="Paper Trading"
        icon={<Wallet size={20} aria-hidden />}
        subtitle="Simulated fills against live order books and bonding curves. No real funds are ever used."
      />
      <ErrorNotice error={error} />
      <div className="card-grid">{accounts?.map((a) => <AccountCard key={a.name} a={a} onReset={reload} />)}</div>
      <Section title="Positions">
        <PositionsTable />
      </Section>
      <Section title="Simulated execution failures">
        <ExecutionFailures />
      </Section>
      <Section title="Sellable-amount protection (partial exits)">
        <ExitProtectionSettings />
      </Section>
      <Section title="Performance analytics">
        <PerformancePanel />
      </Section>
    </div>
  );
}
