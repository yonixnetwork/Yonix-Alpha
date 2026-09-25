"use client";

import Link from "next/link";
import { Settings } from "lucide-react";
import ConfirmButton from "@/components/ConfirmDialog";
import { ErrorNotice, modeClass, PageHeader, Section, Stat } from "@/components/ui";
import { apiPut } from "@/lib/api";
import type { ModesOut } from "@/lib/types";
import { useApi } from "@/lib/useApi";

const GLOBAL_MODES = ["MANUAL", "PAPER", "LIVE"];

export default function SettingsPage() {
  const modes = useApi<ModesOut>("/api/control/modes", undefined, { reloadOn: ["strategy.updated"] });
  const m = modes.data;
  return (
    <div>
      <PageHeader title="Settings" icon={<Settings size={20} aria-hidden />} />
      <ErrorNotice error={modes.error} />
      {m && (
        <>
          <Section title="Execution">
            <div className="stat-grid">
              <Stat label="Global mode">
                <span className={modeClass(m.global_mode)}>{m.global_mode}</span>
              </Stat>
              <Stat label="TRADING_ENABLED">{String(m.env.trading_enabled)}</Stat>
              <Stat label="LIVE_TRADING_ENABLED">{String(m.env.live_trading_enabled)}</Stat>
              <Stat label="PAPER_TRADING">{String(m.env.paper_trading)}</Stat>
              <Stat label="Live permitted">{m.env.live_permitted ? "yes" : "no"}</Stat>
            </div>
            <div className="notice">
              The environment flags live in the server&apos;s .env and cannot be changed from the dashboard. LIVE is refused while
              they are closed, and live execution is not implemented for any venue. To stop everything, use the kill switch or set
              strategies to OFF.
            </div>
            <div className="btn-row" role="group" aria-label="Global mode">
              {GLOBAL_MODES.map((g) => (
                <ConfirmButton
                  key={g}
                  label={g}
                  className={m.global_mode === g ? "btn btn-sm" : "btn btn-ghost btn-sm"}
                  disabled={m.global_mode === g || (g === "LIVE" && !m.env.live_permitted)}
                  title={`Set the global mode to ${g}?`}
                  body={g === "MANUAL" ? "Every entry on every strategy waits for your approval." : g === "PAPER" ? "Strategies run in their own modes, all simulated." : "Refused unless the server's environment locks are open."}
                  danger={g === "LIVE"}
                  onConfirm={async () => {
                    modes.setData(await apiPut<ModesOut>("/api/control/modes/global", { mode: g }));
                  }}
                />
              ))}
            </div>
          </Section>
          <Section title="Where everything else lives">
            <ul className="reason-list">
              <li>
                <Link className="link" href="/dashboard/risk-settings">Risk settings</Link>: per-engine limits, versioned and bounded by hard limits.
              </li>
              <li>
                <Link className="link" href="/dashboard/rules">Blacklist &amp; filters</Link>: token blacklist and custom rules.
              </li>
              <li>
                <Link className="link" href="/dashboard/strategies">Strategies</Link>: per-strategy mode and configuration.
              </li>
              <li>
                <Link className="link" href="/dashboard/notifications">Notifications</Link>: Telegram delivery per kind.
              </li>
              <li>
                <Link className="link" href="/dashboard/paper">Paper trading</Link>: book balances and resets.
              </li>
              <li>Secrets (API keys, RPC URLs, Telegram token) stay in the server&apos;s .env and are never shown here.</li>
            </ul>
          </Section>
        </>
      )}
    </div>
  );
}
