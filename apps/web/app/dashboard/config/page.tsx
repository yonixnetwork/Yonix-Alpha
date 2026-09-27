"use client";

import { SlidersHorizontal } from "lucide-react";
import type { ConfigHealth } from "@/components/RuntimeApply";
import { ErrorNotice, Loading, PageHeader, Section, Stat } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

function pill(status: string): string {
  return status === "SYNCED" || status === "RUNNING" ? "pill pill-ok" : status === "OFF" ? "pill pill-off"
    : status === "NOT_REPORTING" ? "pill pill-warn" : "pill pill-danger";
}

type Effective = { global_mode?: string; modes?: Record<string, string>;
  risk_settings?: Record<string, { source: string; skip_duplicate_names: boolean }> };

/** Configuration Health: the database configuration revision against the
 * revision each running service acknowledged, the stored mode of every
 * module against what the runtime can actually do, and the effective
 * settings each service last read. */
export default function ConfigHealthPage() {
  const { data: h, error } = useApi<ConfigHealth>("/api/config/health", undefined, { refreshMs: 5000 });
  if (error) return <ErrorNotice error={error} />;
  if (!h) return <Loading />;
  const db = h.effective_in_database as Effective;
  const de = (h.services.find((s) => s.service === "decision-engine") as unknown as { effective?: Effective } | undefined)?.effective;
  return (
    <div>
      <PageHeader title="Configuration Health" icon={<SlidersHorizontal size={20} aria-hidden />}
        subtitle="Dashboard settings are stored in the database and applied by the running services without a restart. This page proves which revision each service is running." />
      <div className="stat-grid">
        <Stat label="Runtime config"><span className={pill(h.status)}>{h.status}</span></Stat>
        <Stat label="Database revision">{h.database.revision}</Stat>
        <Stat label="Last change" hint={h.database.change ? `${h.database.change.kind ?? ""} ${h.database.change.path ?? ""}` : undefined}>
          {formatDate(h.database.changed_at)}{h.database.actor ? ` by ${h.database.actor}` : ""}
        </Stat>
      </div>

      <Section title="Services">
        <table className="data-table">
          <thead><tr><th>Service</th><th>Runtime revision</th><th>Status</th><th>Last reload</th><th>Applies</th></tr></thead>
          <tbody>{h.services.map((s) => (
            <tr key={s.service}>
              <td>{s.service}</td>
              <td>{s.revision ?? "—"}{s.revision !== null && <span className="muted"> / db {h.database.revision}</span>}</td>
              <td><span className={pill(s.status)}>{s.status}</span>{s.error && <div className="neg">{s.error}</div>}
                {s.ack_age_seconds !== null && <div className="muted">ack {s.ack_age_seconds}s ago</div>}</td>
              <td>{formatDate(s.loaded_at)}</td>
              <td className="muted">{s.applies}</td>
            </tr>))}
          </tbody>
        </table>
        <p className="muted">SYNCED: the service runs the database revision. OUT OF SYNC: it runs an older one or its reload failed. NOT
          REPORTING: stopped, disabled (e.g. no RPC configured) or not deployed.</p>
      </Section>

      <Section title="Modules — stored setting vs runtime">
        <table className="data-table">
          <thead><tr><th>Module</th><th>Setting</th><th>Runtime</th><th>Reason</th></tr></thead>
          <tbody>{h.modules.map((m) => (
            <tr key={m.module}>
              <td>{m.label ?? m.module}</td>
              <td>{m.mode ?? "—"}</td>
              <td><span className={pill(m.runtime)}>{m.runtime}</span></td>
              <td className="muted">{m.reason ?? ""}</td>
            </tr>))}
          </tbody>
        </table>
      </Section>

      <Section title="Effective settings — database vs decision engine">
        <table className="data-table">
          <thead><tr><th>Setting</th><th>Database</th><th>Decision engine (last read)</th></tr></thead>
          <tbody>
            <tr><td>Global mode</td><td>{db.global_mode}</td><td>{de?.global_mode ?? "not reporting"}</td></tr>
            {Object.entries(db.modes ?? {}).map(([k, v]) => (
              <tr key={k}><td>{k} mode</td><td>{v}</td>
                <td className={de?.modes && de.modes[k] !== v ? "neg" : undefined}>{de?.modes?.[k] ?? "—"}</td></tr>))}
            {Object.entries(db.risk_settings ?? {}).map(([k, v]) => (
              <tr key={k}><td>{k} risk settings</td>
                <td>{v.source} · duplicate names {v.skip_duplicate_names ? "REJECTED" : "ALLOWED"}</td>
                <td>{de?.risk_settings?.[k] ? `${de.risk_settings[k].source} · duplicate names ${de.risk_settings[k].skip_duplicate_names ? "REJECTED" : "ALLOWED"}` : "—"}</td></tr>))}
          </tbody>
        </table>
        <p className="muted">An engine with its own saved risk settings (source “solana_fresh vN”) does not use GLOBAL at all; edit that
          engine&apos;s scope, or use “apply to engines” on the Risk Settings page.</p>
      </Section>

      <Section title="Changes that still need the server">
        <ul>{h.restart_required_for.map((r) => <li key={r} className="muted">{r}</li>)}</ul>
      </Section>
    </div>
  );
}
