"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { apiGet, CONFIG_SAVED_EVENT, lastConfigRevision } from "@/lib/api";

export interface ServiceSync {
  service: string; applies: string; status: "SYNCED" | "OUT_OF_SYNC" | "NOT_REPORTING"; revision: number | null;
  loaded_at: string | null; ack_age_seconds: number | null; error: string | null;
}
export interface ConfigHealth {
  database: { revision: number; changed_at: string | null; change: Record<string, string> | null; actor: string | null };
  status: string;
  services: ServiceSync[];
  modules: { module: string; label: string | null; mode: string | null; runtime: string; reason: string | null }[];
  effective_in_database: Record<string, unknown>;
  restart_required_for: string[];
}

type Phase = { kind: "idle" } | { kind: "applying"; revision: number } |
  { kind: "applied"; revision: number; synced: number } |
  { kind: "pending"; revision: number; behind: ServiceSync[]; silent: ServiceSync[] };

const TIMEOUT_MS = 30_000;

/** Whether a saved setting is actually running. A save is only shown as
 * APPLIED once every reporting service acknowledged its configuration
 * revision; otherwise SAVED — RUNTIME UPDATE PENDING with the services
 * that are behind. `inline` renders next to a form's save button. */
export default function RuntimeApply({ inline = false, since }: { inline?: boolean; since?: number | null }) {
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function watch(revision: number) {
      if (timer.current) clearTimeout(timer.current);
      setPhase({ kind: "applying", revision });
      const started = Date.now();
      const tick = async () => {
        if (cancelled) return;
        try {
          const h = await apiGet<ConfigHealth>("/api/config/health");
          const reporting = h.services.filter((s) => s.status !== "NOT_REPORTING");
          const behind = reporting.filter((s) => (s.revision ?? -1) < revision || s.error);
          if (reporting.length > 0 && behind.length === 0) {
            setPhase({ kind: "applied", revision, synced: reporting.length });
            return;
          }
          if (Date.now() - started > TIMEOUT_MS) {
            setPhase({ kind: "pending", revision, behind, silent: h.services.filter((s) => s.status === "NOT_REPORTING") });
            return;
          }
        } catch {
          if (Date.now() - started > TIMEOUT_MS) {
            setPhase({ kind: "pending", revision, behind: [], silent: [] });
            return;
          }
        }
        timer.current = setTimeout(tick, 1500);
      };
      await tick();
    }
    const onSaved = (e: Event) => void watch((e as CustomEvent<{ revision: number }>).detail.revision);
    window.addEventListener(CONFIG_SAVED_EVENT, onSaved);
    if (since !== undefined && since !== null) void watch(since);
    else if (inline && lastConfigRevision !== null) void watch(lastConfigRevision);
    return () => {
      cancelled = true;
      window.removeEventListener(CONFIG_SAVED_EVENT, onSaved);
      if (timer.current) clearTimeout(timer.current);
    };
  }, [inline, since]);

  if (phase.kind === "idle") return inline ? null : <Link href="/dashboard/config" className="pill pill-off">config</Link>;
  if (phase.kind === "applying") {
    return <span className="pill pill-warn" role="status">SAVED — applying rev {phase.revision}…</span>;
  }
  if (phase.kind === "applied") {
    return (
      <span className="pill pill-ok" role="status" title={`${phase.synced} services acknowledged`}>
        APPLIED — runtime rev {phase.revision} ({phase.synced} services)
      </span>
    );
  }
  const names = phase.behind.map((s) => `${s.service}${s.error ? ` (reload error: ${s.error})` : ` at rev ${s.revision ?? "—"}`}`);
  return (
    <span role="alert">
      <Link href="/dashboard/config" className="pill pill-danger">SAVED — RUNTIME UPDATE PENDING (rev {phase.revision})</Link>
      {inline && (
        <span className="muted">
          {" "}
          {names.length ? `Behind: ${names.join(", ")}.` : "No service acknowledged it yet."}
          {phase.silent.length ? ` Not reporting: ${phase.silent.map((s) => s.service).join(", ")}.` : ""}
        </span>
      )}
    </span>
  );
}
