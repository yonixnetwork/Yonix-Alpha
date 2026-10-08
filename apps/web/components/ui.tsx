"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { AlertTriangle, CheckCircle2, CircleDashed, CircleOff, Clock, MinusCircle } from "lucide-react";
import type { ConnState } from "@/lib/cc";

export function stateClass(state: string | undefined | null): string {
  switch (state) {
    case "CONNECTED":
      return "pill pill-ok";
    case "DEGRADED":
    case "STALE":
      return "pill pill-warn";
    case "UNAVAILABLE":
      return "pill pill-danger";
    default:
      return "pill pill-off";
  }
}

const STATE_ICON: Record<ConnState, typeof CheckCircle2> = {
  CONNECTED: CheckCircle2,
  DEGRADED: AlertTriangle,
  STALE: Clock,
  UNAVAILABLE: CircleOff,
  "NOT CONFIGURED": MinusCircle,
  UNKNOWN: CircleDashed,
};

export function StatePill({ state, label }: { state: ConnState | string; label?: string }) {
  const Icon = STATE_ICON[state as ConnState] ?? CircleDashed;
  return (
    <span className={stateClass(state)}>
      <Icon size={12} aria-hidden /> {label ?? state}
    </span>
  );
}

export function modeClass(mode: string | undefined): string {
  if (mode === "LIVE" || mode === "AUTO") return "pill pill-danger";
  if (mode === "OFF") return "pill pill-off";
  if (mode === "MANUAL") return "pill pill-warn";
  return "pill pill-ok";
}

export function num(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/** A signed amount in its own currency, colored by sign. */
export function Money({ value, currency, digits = 4 }: { value: string | null | undefined; currency?: string | null; digits?: number }) {
  const n = num(value);
  if (n === null) return <span className="muted">—</span>;
  const cls = n > 0 ? "pos" : n < 0 ? "neg" : "";
  return (
    <span className={cls}>
      {n.toLocaleString(undefined, { maximumFractionDigits: digits })}
      {currency ? <span className="unit"> {currency}</span> : null}
    </span>
  );
}

export function Pct({ value, digits = 1 }: { value: number | string | null | undefined; digits?: number }) {
  const n = num(value);
  return n === null ? <span className="muted">—</span> : <span>{(n * 100).toFixed(digits)}%</span>;
}

export function Stat({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{children}</div>
      {hint && <div className="form-hint">{hint}</div>}
    </div>
  );
}

export function Section({ title, actions, children, id }: { title: string; actions?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <section className="cc-section" aria-labelledby={id}>
      <div className="cc-section-head">
        <h2 className="section-title" id={id}>
          {title}
        </h2>
        {actions}
      </div>
      {children}
    </section>
  );
}

export function ErrorNotice({ error }: { error: string | null | undefined }) {
  if (!error) return null;
  return (
    <div className="error" role="alert">
      {error}
    </div>
  );
}

export function Loading({ what = "Loading…" }: { what?: string }) {
  return (
    <div className="empty-state" role="status" aria-live="polite">
      {what}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty-state">{children}</div>;
}

export function PageHeader({ title, icon, children, subtitle }: { title: string; icon?: ReactNode; children?: ReactNode; subtitle?: ReactNode }) {
  return (
    <div className="page-header">
      <div>
        <h1 className="page-title">
          {icon}
          {title}
        </h1>
        {subtitle && <div className="muted">{subtitle}</div>}
      </div>
      {children && <div className="btn-row">{children}</div>}
    </div>
  );
}

export function TokenLink({ mint, label }: { mint: string; label?: string | null }) {
  return (
    <Link href={`/dashboard/tokens/${mint}`} className="mono link">
      {label || `${mint.slice(0, 4)}…${mint.slice(-4)}`}
    </Link>
  );
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  if (seconds < 172800) return `${(seconds / 3600).toFixed(1)} h`;
  return `${(seconds / 86400).toFixed(1)} d`;
}

export type ReviewMeta = {
  cached_at?: string; age_s?: number; review_status?: string; refreshing?: boolean; deferred?: string;
  refresh_error?: string; error?: string; message?: string;
};

/** True once a background-computed review result exists (apps/api
 * review_cache); before that the response carries only its status. */
export const reviewReady = (d: ReviewMeta | null | undefined): boolean => !!d?.cached_at;

/** A background-computed review result's status (apps/api review_cache):
 * when it was calculated, its age and CURRENT / STALE, or RUNNING / PENDING
 * / FAILED while there is none. Refresh asks for a new calculation. */
export function ReviewStatus({ data, onRefresh }: { data: ReviewMeta | null | undefined; onRefresh?: () => void }) {
  if (!data) return null;
  const st = data.review_status ?? (data.cached_at ? "CURRENT" : "UNKNOWN");
  if (!data.cached_at) {
    return (
      <div className={st === "FAILED" ? "error" : "notice"} role="status">
        Status: <b>{st}</b> — {st === "FAILED" ? data.error : data.deferred ? `waiting: ${data.deferred}` : data.message}
        {onRefresh && st !== "RUNNING" && <> <button className="btn btn-ghost btn-sm" onClick={onRefresh}>Calculate now</button></>}
      </div>
    );
  }
  const mins = Math.max(0, Math.round((data.age_s ?? (Date.now() - new Date(data.cached_at).getTime()) / 1000) / 60));
  return (
    <p className="muted small">
      Last calculated: {new Date(data.cached_at).toLocaleTimeString()} · Age: {mins} min · Status: <b>{st}</b>
      {data.refreshing && " (a newer result is being calculated in the background)"}
      {data.deferred && ` (refresh waiting: ${data.deferred})`}
      {data.refresh_error && <span className="pill pill-warn" title={data.refresh_error}> last refresh failed</span>}
      {onRefresh && !data.refreshing && <> <button className="btn btn-ghost btn-sm" onClick={onRefresh}>Refresh</button></>}
    </p>
  );
}
