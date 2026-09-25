"use client";

import Link from "next/link";
import { useState } from "react";
import { LayoutDashboard } from "lucide-react";
import ConfirmButton from "@/components/ConfirmDialog";
import DecisionsTable from "@/components/DecisionsTable";
import PositionsTable from "@/components/PositionsTable";
import { ErrorNotice, Loading, modeClass, Money, PageHeader, Section, Stat, StatePill } from "@/components/ui";
import { apiPost } from "@/lib/api";
import type { HealthOut, StrategyOut, SummaryOut } from "@/lib/cc";
import type { KillSwitchStatus } from "@/lib/types";
import { useApi } from "@/lib/useApi";

function KillSwitch() {
  const { data, reload } = useApi<KillSwitchStatus>("/api/risk/kill-switch", undefined, { refreshMs: 15000 });
  const [reason, setReason] = useState("");
  if (!data) return null;
  return (
    <div className="card kill-switch-panel">
      <div className="status-label">Kill switch</div>
      <div>
        <span className={data.engaged ? "pill pill-danger" : "pill pill-ok"}>{data.engaged ? "ENGAGED" : "CLEAR"}</span>{" "}
        {data.reason && <span className="muted">{data.reason}</span>}
      </div>
      {data.engaged ? (
        <ConfirmButton
          label="Disengage"
          className="btn btn-sm"
          title="Disengage the kill switch?"
          body="Engines resume opening paper positions under their current modes."
          onConfirm={async () => {
            await apiPost("/api/risk/kill-switch/disengage");
            reload();
          }}
        />
      ) : (
        <>
          <label htmlFor="ks-reason" className="sr-only">
            Reason
          </label>
          <textarea id="ks-reason" placeholder="Reason (required)" value={reason} onChange={(e) => setReason(e.target.value)} />
          <ConfirmButton
            label="Engage kill switch"
            danger
            disabled={!reason.trim()}
            title="Engage the kill switch?"
            body="Every engine stops opening positions immediately; the grid is flattened on its next tick."
            onConfirm={async () => {
              await apiPost("/api/risk/kill-switch/engage", { reason });
              setReason("");
              reload();
            }}
          />
        </>
      )}
    </div>
  );
}

export default function DashboardPage() {
  const summary = useApi<SummaryOut>("/api/summary", undefined, {
    refreshMs: 30000,
    reloadOn: ["balance.updated", "trade.created", "trade.closed"],
  });
  const health = useApi<HealthOut>("/api/system/health", undefined, { refreshMs: 30000 });
  const strategies = useApi<StrategyOut[]>("/api/strategies", undefined, { reloadOn: ["strategy.updated"], refreshMs: 60000 });
  const s = summary.data;
  return (
    <div>
      <PageHeader title="Dashboard" icon={<LayoutDashboard size={20} aria-hidden />} subtitle="Paper trading only. No real funds, no live orders." />
      <ErrorNotice error={summary.error} />
      {!s && summary.loading && <Loading />}
      {s && (
        <div className="card-grid">
          {s.accounts.map((a) => (
            <div className="card" key={a.name}>
              <div className="status-label">
                {a.name} · {a.currency}
              </div>
              <div className="stat-value big">
                {Number(a.equity ?? a.balance).toLocaleString(undefined, { maximumFractionDigits: 4 })} <span className="unit">{a.currency}</span>
              </div>
              <dl className="kv">
                <dt>Available</dt>
                <dd>{Number(a.available).toLocaleString(undefined, { maximumFractionDigits: 4 })}</dd>
                <dt>Open positions</dt>
                <dd>{a.open_positions}</dd>
                <dt>PnL today</dt>
                <dd>
                  <Money value={a.realized_pnl_today} />
                </dd>
                <dt>PnL since reset</dt>
                <dd>
                  <Money value={a.realized_pnl_since_reset} />
                </dd>
              </dl>
            </div>
          ))}
          <KillSwitch />
        </div>
      )}
      <Section title="Connections" actions={<Link className="btn btn-ghost btn-sm" href="/dashboard/health">Details</Link>}>
        {health.data && (
          <div className="chip-row">
            {health.data.connections.map((c) => (
              <span key={c.name} title={c.detail} className="chip">
                {c.name} <StatePill state={c.state} />
              </span>
            ))}
          </div>
        )}
      </Section>
      <Section title="Strategies" actions={<Link className="btn btn-ghost btn-sm" href="/dashboard/strategies">All</Link>}>
        <div className="stat-grid">
          {strategies.data
            ?.filter((x) => x.kind !== "venue")
            .map((x) => (
              <Stat key={x.name} label={x.label}>
                {x.mode ? <span className={modeClass(x.effective_mode)}>{x.effective_mode}</span> : <span className="pill pill-off">analytics</span>}{" "}
                {x.open_positions ? <span className="muted">{x.open_positions} open</span> : null}
              </Stat>
            ))}
        </div>
      </Section>
      <Section title="Open positions">
        <PositionsTable />
      </Section>
      <Section title="Latest decisions" actions={<Link className="btn btn-ghost btn-sm" href="/dashboard/decisions">All</Link>}>
        <DecisionsTable limit={10} />
      </Section>
    </div>
  );
}
