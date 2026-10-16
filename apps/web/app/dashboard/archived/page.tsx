"use client";

// Archived Engines. Read-only: this page has no trading control. It shows the
// archived Memecoin engine's current state from the existing summary API and
// links to its pages, where its own controls still act on the running engine.

import Link from "next/link";
import { Archive } from "lucide-react";
import { MEMECOIN_GROUPS } from "@/components/archivedEngines";
import { ErrorNotice, Loading, PageHeader, Section, modeClass } from "@/components/ui";
import type { SummaryOut } from "@/lib/cc";
import { formatDecimal } from "@/lib/format";
import { useApi } from "@/lib/useApi";

const MEMECOIN_ACCOUNTS = ["solana", "live_solana", "evm_bsc", "evm_robinhood"];

export default function ArchivedEnginesPage() {
  const { data, error } = useApi<SummaryOut>("/api/summary", undefined, { refreshMs: 30000 });
  return (
    <div>
      <PageHeader title="Archived Engines" icon={<Archive size={20} aria-hidden />}
        subtitle="Engines kept out of the main menu. Their code, settings, data and history are preserved." />

      <Section title="Memecoin Trading (Solana, BSC, Robinhood Chain)">
        <p>
          Archived on 2026-10-16 so that the main menu shows regular trading. <strong>Only the menu changed.</strong> The
          Memecoin workers keep running exactly as before, with the same settings, risk limits, open positions and
          protection. Nothing was disabled, stopped, reset or deleted.
        </p>
        {error && <ErrorNotice error={error} />}
        {!data && !error && <Loading />}
        {data && (
          <div className="metric-grid">
            <div className="metric"><span className="term-label">Global mode</span>
              <span className="term-value mono"><span className={modeClass(data.global_mode)}>{data.global_mode}</span></span></div>
            <div className="metric"><span className="term-label">Open positions</span>
              <span className="term-value mono">{data.open_positions}</span></div>
            <div className="metric"><span className="term-label">Platform kill switch</span>
              <span className="term-value mono">{data.kill_switch ? <span className="pill pill-danger">ON</span> : "off"}</span></div>
            {data.accounts.filter((a) => MEMECOIN_ACCOUNTS.includes(a.name)).map((a) => (
              <div className="metric" key={a.name}><span className="term-label">{a.name} balance</span>
                <span className="term-value mono">{formatDecimal(a.balance, 4)} {a.currency}</span></div>
            ))}
          </div>
        )}
      </Section>

      <Section title="Hide, disable and stop are different actions">
        <ul>
          <li><strong>Hide from the main menu</strong> (done): only the navigation changes. The engine keeps trading in
            its current mode and keeps managing its open positions.</li>
          <li><strong>Disable new entries</strong> (not done): set the Memecoin strategies or the global mode to OFF on
            its Settings page. Position monitoring and exits keep running.</li>
          <li><strong>Stop its workers</strong> (not done): a server action (<code>docker compose stop</code>). Stopping
            them also stops the monitoring of open positions, so it must never be done while a position is open.</li>
        </ul>
        <p>
          Restore to the main menu: set <code>MEMECOIN_ARCHIVED</code> to <code>false</code> in
          <code> apps/web/components/archivedEngines.ts</code> and redeploy the web app. Nothing else needs restoring.
        </p>
      </Section>

      <Section title="Memecoin pages">
        <p className="muted">The controls on these pages act on the running Memecoin engine.</p>
        {MEMECOIN_GROUPS.map((g) => (
          <div key={g.title || "overview"}>
            {g.title && <div className="nav-group-title">{g.title}</div>}
            <ul>
              {g.items.map((i) => <li key={i.href}><Link href={i.href}>{i.label}</Link></li>)}
            </ul>
          </div>
        ))}
      </Section>
    </div>
  );
}
