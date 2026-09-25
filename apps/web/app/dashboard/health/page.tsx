"use client";

import Link from "next/link";
import { Server } from "lucide-react";
import { ErrorNotice, Loading, PageHeader, Section, Stat, StatePill } from "@/components/ui";
import type { ConfigValidationOut, HealthOut } from "@/lib/cc";
import { useLiveStatus } from "@/lib/events";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

const CATEGORY_ORDER = ["infrastructure", "service", "solana", "exchange", "execution", "control_api", "ml", "realtime"];
const CATEGORY_LABEL: Record<string, string> = {
  infrastructure: "Infrastructure",
  service: "Services",
  solana: "Solana",
  exchange: "Exchanges & market data",
  execution: "Live execution",
  control_api: "External bot control APIs",
  ml: "ML",
  realtime: "Realtime",
};
const CONFIG_CLASS: Record<string, string> = { READY: "pill pill-ok", DISABLED: "pill pill-off", CONFIGURATION_ERROR: "pill pill-danger" };

export default function HealthPage() {
  const { data, error, loading } = useApi<HealthOut>("/api/system/health", undefined, { refreshMs: 15000, reloadOn: ["system.health.updated"] });
  const obs = useApi<Record<string, any>>("/api/system/observability", undefined, { refreshMs: 30000 });
  const cfg = useApi<ConfigValidationOut>("/api/system/config-validation", undefined, { refreshMs: 60000 });
  const live = useLiveStatus();
  return (
    <div>
      <PageHeader title="System Health" icon={<Server size={20} aria-hidden />} subtitle="Every state comes from a probe, a heartbeat or recorded calls; no evidence is UNKNOWN.">
        <Link className="btn btn-ghost btn-sm" href="/dashboard/events">
          System events
        </Link>
      </PageHeader>
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {data && (
        <>
          <div className="notice">
            Overall: <StatePill state={data.overall} /> · this browser&apos;s realtime link: <b>{live.status}</b>
            {live.lastEventAt && <> (last event {formatDate(live.lastEventAt)})</>}
          </div>
          {CATEGORY_ORDER.map((cat) => {
            const items = data.connections.filter((c) => c.category === cat);
            if (!items.length) return null;
            return (
              <Section key={cat} title={CATEGORY_LABEL[cat] ?? cat}>
                <div className="table-wrap">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>Name</th>
                        <th>State</th>
                        <th>Detail</th>
                      </tr>
                    </thead>
                    <tbody>
                      {items.map((c) => (
                        <tr key={c.name}>
                          <td>{c.name}</td>
                          <td>
                            <StatePill state={c.state} />
                          </td>
                          <td className="muted small">
                            {c.detail}
                            {typeof c.latency_ms === "number" && ` · ${c.latency_ms} ms`}
                            {typeof c.rss_mb === "number" && ` · ${c.rss_mb} MB RSS`}
                            {typeof c.last_error === "string" && c.last_error && ` · last error: ${c.last_error}`}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Section>
            );
          })}
        </>
      )}
      {cfg.data && (
        <Section title="Configuration (per module)">
          <p className="muted small">{cfg.data.note}. A module in CONFIGURATION_ERROR cannot be switched to AUTO/LIVE; disabled modules never block the others.</p>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Module</th>
                  <th>Status</th>
                  <th>Mode</th>
                  <th>Problems</th>
                  <th>Live would still need</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(cfg.data.modules).map(([name, m]) => (
                  <tr key={name}>
                    <td>{m.label}</td>
                    <td>
                      <span className={CONFIG_CLASS[m.status] ?? "pill pill-off"}>{m.status.replace("_", " ")}</span>
                    </td>
                    <td>{m.mode ?? "—"}</td>
                    <td className="small">{[...m.errors, ...m.warnings].join("; ") || "—"}</td>
                    <td className="muted small">{m.live_missing.join("; ") || (m.live_ready ? "nothing — configured" : "—")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}
      {obs.data && (
        <Section title="Observability (24 h)">
          <div className="stat-grid">
            <Stat label="Dashboard WebSocket clients">{obs.data.websocket_clients}</Stat>
            <Stat label="Redis memory">
              {obs.data.redis_memory.used} / {obs.data.redis_memory.max || "no limit"}
            </Stat>
            <Stat label="Errors (by service)">
              {Object.entries(obs.data.errors_24h).map(([k, v]) => `${k}: ${v}`).join(", ") || "none"}
            </Stat>
            <Stat label="Decisions">{Object.entries(obs.data.decisions_24h).map(([k, v]) => `${k}: ${v}`).join(", ") || "none"}</Stat>
            <Stat label="Notifications">{Object.entries(obs.data.notifications_24h).map(([k, v]) => `${k}: ${v}`).join(", ") || "none"}</Stat>
            <Stat label="Quarantined ML samples">{Object.values(obs.data.data_quality_24h).reduce((a: number, b) => a + Number(b), 0)}</Stat>
          </div>
          <div className="muted small">
            Realtime events published since Redis started:{" "}
            {Object.entries(obs.data.events_published).map(([k, v]) => `${k} ${v}`).join(" · ") || "none"}
          </div>
        </Section>
      )}
    </div>
  );
}
