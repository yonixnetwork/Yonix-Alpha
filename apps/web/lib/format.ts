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
