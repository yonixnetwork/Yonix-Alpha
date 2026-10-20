"use client";

// Archived Engines. Read-only: this page has no trading control. It shows the
// archived Crypto / Exchange Trading engine's current state from its own
// status API and links to its pages, where its own controls still act on the
// Crypto Trading worker.

import Link from "next/link";
import { Archive } from "lucide-react";
import { CRYPTO_GROUPS } from "@/components/archivedEngines";
import { ErrorNotice, Loading, PageHeader, Section } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

interface CryptoStatus {
  schema_ready: boolean;
  worker_running: boolean;
  engine: null | { enabled: boolean; emergency_stop: boolean; updated_at: string | null; updated_by: string | null };
  strategies: { id: string; config: null | { enabled: boolean; mode: string } }[];
}

export default function ArchivedEnginesPage() {
  const { data, error } = useApi<CryptoStatus>("/api/crypto/status", undefined, { refreshMs: 30000 });
  const enabled = (data?.strategies ?? []).filter((s) => s.config?.enabled).map((s) => s.id);
  return (
    <div>
      <PageHeader title="Archived Engines" icon={<Archive size={20} aria-hidden />}
        subtitle="Engines kept out of the main menu. Their code, settings, data and history are preserved." />

      <Section title="Crypto / Exchange Trading (Binance, Bybit, Binance.US)">
        <p>
          Archived on 2026-10-20: YonixAlpha is a Solana memecoin platform again. <strong>Only the menu changed.</strong>{" "}
          No code, table, setting or history was deleted, and no Memecoin page, worker or trade path uses this engine.
          A snapshot of the exchange work is kept on the git branch <code>archive/crypto-exchange-trading-2026-10-20</code>.
        </p>
        {error && <ErrorNotice error={error} />}
        {!data && !error && <Loading />}
        {data && (
          <div className="metric-grid">
            <div className="metric"><span className="term-label">Engine</span>
              <span className="term-value mono">{data.engine ? (data.engine.enabled ? "ON" : "off") : "not set up"}</span></div>
            <div className="metric"><span className="term-label">Emergency stop</span>
              <span className="term-value mono">{data.engine?.emergency_stop ? "ON" : "off"}</span></div>
            <div className="metric"><span className="term-label">Worker</span>
              <span className="term-value mono">{data.worker_running ? "running" : "not running"}</span></div>
            <div className="metric"><span className="term-label">Enabled strategies</span>
              <span className="term-value mono">{enabled.length ? enabled.join(", ") : "none"}</span></div>
            {data.engine?.updated_at && (
              <div className="metric"><span className="term-label">Last engine change</span>
                <span className="term-value mono">{formatDate(data.engine.updated_at)} by {data.engine.updated_by ?? "-"}</span></div>
            )}
          </div>
        )}
      </Section>

      <Section title="Hide, turn off and stop are different actions">
        <ul>
          <li><strong>Hide from the main menu</strong> (done): only the navigation changes.</li>
          <li><strong>Turn the engine off</strong> (your decision): Engine (Strategy Lab) &gt; Turn engine off. New demo
            entries stop; open demo positions keep their stop and target protection. With the engine off and nothing
            open, its worker exits by itself and frees its memory.</li>
          <li><strong>Exchange live orders</strong>: locked in code (<code>LIVE_ORDERS_UNLOCKED = False</code>); archiving
            does not change that.</li>
        </ul>
        <p>
          Restore to the main menu: set <code>CRYPTO_ARCHIVED</code> to <code>false</code> in
          <code> apps/web/components/archivedEngines.ts</code> and redeploy the web app. Nothing else needs restoring.
        </p>
      </Section>

      <Section title="Crypto / Exchange Trading pages">
        <p className="muted">The controls on these pages act on the Crypto Trading worker.</p>
        {CRYPTO_GROUPS.map((g) => (
          <div key={g.title}>
            <div className="nav-group-title">{g.title}</div>
            <ul>
              {g.items.map((i) => <li key={i.href}><Link href={i.href}>{i.label}</Link></li>)}
            </ul>
          </div>
        ))}
      </Section>
    </div>
  );
}
