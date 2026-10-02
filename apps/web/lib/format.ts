export function formatDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

export function formatDecimal(value: string | null, digits = 6): string {
  if (value === null) return "—";
  const n = Number(value);
  if (Number.isNaN(n)) return value;
  return n.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export function decisionPillClass(decision: string): string {
  if (decision === "LONG" || decision === "SHORT") return "pill pill-ok";
  if (decision === "NO_TRADE") return "pill pill-danger";
  return "pill pill-off";
}

export function candidateStatePillClass(state: string): string {
  if (state === "closed") return "pill pill-ok";
  if (state === "rejected") return "pill pill-danger";
  return "pill pill-off";
}

export function boolPillClass(value: boolean): string {
  return value ? "pill pill-ok" : "pill pill-danger";
}

export function formatState(state: string | null | undefined): string {
  // state_history and state come from a JSONB column, so nothing at the
  // database level guarantees they are present or strings. Calling
  // .replace() on them directly used to throw an uncaught TypeError that
  // white-screened the whole candidate detail page for one malformed row.
  if (typeof state !== "string" || state.length === 0) return "unknown";
  return state.replace(/_/g, " ");
}

export function gateDecisionPillClass(decision: string): string {
  if (decision === "EXECUTE") return "pill pill-ok";
  if (decision === "REDUCE_SIZE") return "pill pill-ok";
  if (decision === "WAIT" || decision === "REQUIRE_MANUAL_APPROVAL") return "pill pill-warn";
  if (decision === "REJECT" || decision === "NO_TRADE") return "pill pill-danger";
  return "pill pill-off";
}

export function formatPct(value: string | number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (Number.isNaN(n)) return String(value);
  return `${(n * 100).toFixed(digits)}%`;
}

export function formatBps(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  return Number.isNaN(n) ? value : `${(n / 100).toFixed(2)}%`;
}

/** USD in compact form for market caps and liquidity: $950, $9.5K, $95.3K,
 * $953K, $1.2M, $12.4M, $1.05B. "—" when the value is missing or not a number. */
export function formatUsdCompact(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  const sign = n < 0 ? "-" : "";
  const a = Math.abs(n);
  // master §61: $950, $9.5K, $300K, $1.2M, $12.4M, $1.05B
  const trim = (t: string) => (t.includes(".") ? t.replace(/0+$/, "").replace(/\.$/, "") : t);
  const unit = (v: number, suffix: string) =>
    `${sign}$${v < 10 ? trim(v.toFixed(2)) : v < 100 ? trim(v.toFixed(1)) : v.toFixed(0)}${suffix}`;
  if (a >= 1e9) return unit(a / 1e9, "B");
  if (a >= 1e6) return unit(a / 1e6, "M");
  if (a >= 1e3) return unit(a / 1e3, "K");
  return `${sign}$${a < 10 ? a.toFixed(2) : a.toFixed(0)}`;
}

/** SOL amount with thousands grouping and at most `digits` decimals. */
export function formatSol(value: number | string | null | undefined, digits = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${n.toLocaleString("en-US", { maximumFractionDigits: digits })} SOL`;
}
