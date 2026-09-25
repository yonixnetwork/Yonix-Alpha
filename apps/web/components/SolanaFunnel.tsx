"use client";

import { ErrorNotice, Stat, StatePill } from "@/components/ui";
import { useApi } from "@/lib/useApi";
import type { PipelineOut } from "@/lib/types";

/** Live pump.fun stream and discovery funnel counters. */
export default function SolanaFunnel() {
  const { data, error } = useApi<PipelineOut>("/api/control/pipeline", undefined, { refreshMs: 15000, reloadOn: ["token.discovered"] });
  if (error) return <ErrorNotice error={error} />;
  if (!data) return null;
  const age = data.stream.heartbeat_age_seconds as number | null;
  const state = age === null ? "UNKNOWN" : age <= 60 ? "CONNECTED" : age <= 300 ? "STALE" : "UNAVAILABLE";
  const counters = (data.stream.counters ?? {}) as Record<string, number>;
  return (
    <div className="card">
      <div className="status-label">
        pump.fun stream <StatePill state={state} label={age === null ? "no data" : `${state} · ${age}s ago`} />
      </div>
      <div className="stat-grid">
        {Object.entries(counters).map(([k, v]) => (
          <Stat key={k} label={k.replace(/_/g, " ")}>
            {v.toLocaleString()}
          </Stat>
        ))}
        {Object.entries(data.funnel ?? {}).map(([k, v]) => (
          <Stat key={`f-${k}`} label={`funnel: ${k.replace(/_/g, " ")}`}>
            {String(v)}
          </Stat>
        ))}
        {Object.entries(data.decisions_24h ?? {}).map(([k, v]) => (
          <Stat key={`d-${k}`} label={`24h ${k}`}>
            {v}
          </Stat>
        ))}
      </div>
    </div>
  );
}
