"use client";

import { Minus, TrendingDown, TrendingUp } from "lucide-react";

/** The real-time PnL view of one position (API field `pnl`, built by
 * yonixalpha_core.position_pnl): PROFIT / LOSS with its percentage, never a
 * bare OPEN; PNL_UNAVAILABLE with the reason when no price mark exists.
 * Green for positive, red for negative, neutral otherwise; icons, no emojis. */
export interface PnlView {
  outcome: "PROFIT" | "LOSS" | "BREAKEVEN" | "PNL_UNAVAILABLE";
  tone: "positive" | "negative" | "neutral";
  reason: string | null;
  net: string | null;
  net_pct: string | null;
  entry: string | null;
  current: string | null;
  price_status: "LIVE" | "STALE" | "UNAVAILABLE" | "CLOSED";
  price_age_seconds: number | null;
  quantity: string | null;
  entry_cost: string | null;
  value: string | null;
  unrealized: string | null;
  unrealized_pct: string | null;
  realized: string | null;
  fees: string | null;
  peak_pct: string | null;
  drawdown_pct: string | null;
  basis: string;
}

const TONE_CLASS = { positive: "pos", negative: "neg", neutral: "muted" } as const;

function signed(v: string | null, suffix = ""): string {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return `${n > 0 ? "+" : ""}${v}${suffix}`;
}

/** Quote-coin amount: significant digits, never exponent notation. */
function amount(v: string | null, currency?: string): string {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  const s = n.toLocaleString(undefined, { maximumSignificantDigits: 6, maximumFractionDigits: 12 });
  return `${n > 0 ? "+" : ""}${s}${currency ? ` ${currency}` : ""}`;
}

function plain(v: string | null): string {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumSignificantDigits: 6, maximumFractionDigits: 18 }) : "—";
}

export function PnlIcon({ tone, size = 14 }: { tone: PnlView["tone"]; size?: number }) {
  const Icon = tone === "positive" ? TrendingUp : tone === "negative" ? TrendingDown : Minus;
  return <Icon size={size} aria-hidden />;
}

/** "+12.40% PROFIT" with the net amount underneath; the compact table cell. */
export function PnlOutcome({ pnl, currency, showAmount = true }: { pnl: PnlView | null | undefined; currency?: string; showAmount?: boolean }) {
  if (!pnl) return <span className="muted">—</span>;
  if (pnl.outcome === "PNL_UNAVAILABLE") {
    return (
      <span className="muted" title={pnl.reason ?? undefined}>
        <PnlIcon tone="neutral" /> PnL unavailable{pnl.reason ? <span className="small"> ({pnl.reason})</span> : null}
      </span>
    );
  }
  const cls = TONE_CLASS[pnl.tone];
  return (
    <span className={cls} title={`Net PnL; basis: ${pnl.basis}`}>
      <strong>
        <PnlIcon tone={pnl.tone} /> {signed(pnl.net_pct, "%")} {pnl.outcome}
      </strong>
      {pnl.price_status === "STALE" && <span className="pill pill-warn" title={`last mark ${pnl.price_age_seconds ?? "?"}s ago`}> stale price</span>}
      {showAmount && <div className="small">{amount(pnl.net, currency)}</div>}
    </span>
  );
}

/** Every §59 field of one position. */
export function PnlDetails({ pnl, currency }: { pnl: PnlView | null | undefined; currency?: string }) {
  if (!pnl) return <span className="muted">—</span>;
  const tone = (v: string | null) => (v === null ? "muted" : Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : "");
  const cur = currency ? ` ${currency}` : "";
  return (
    <div>
      <PnlOutcome pnl={pnl} currency={currency} />
      <dl className="term-kv small" style={{ marginTop: 6 }}>
        <dt>Entry</dt><dd className="mono">{plain(pnl.entry)}</dd>
        <dt>{pnl.price_status === "CLOSED" ? "Exit" : "Current"}</dt>
        <dd className="mono">{plain(pnl.current)} {pnl.price_status !== "CLOSED" && <span className="muted">({pnl.price_status.toLowerCase()})</span>}</dd>
        <dt>Quantity</dt><dd className="mono">{plain(pnl.quantity)}</dd>
        <dt>Value</dt><dd className="mono">{pnl.value === null ? "—" : `${plain(pnl.value)}${cur}`}</dd>
        <dt>Unrealized PnL</dt><dd className={`mono ${tone(pnl.unrealized)}`}>{amount(pnl.unrealized, currency)} {pnl.unrealized_pct !== null && `(${signed(pnl.unrealized_pct, "%")})`}</dd>
        <dt>Realized PnL</dt><dd className={`mono ${tone(pnl.realized)}`}>{amount(pnl.realized, currency)}</dd>
        <dt>Fees</dt><dd className="mono">{pnl.fees === null ? "—" : `${plain(pnl.fees)}${cur}`}</dd>
        <dt>Net PnL</dt><dd className={`mono ${tone(pnl.net)}`}>{amount(pnl.net, currency)} {pnl.net_pct !== null && `(${signed(pnl.net_pct, "%")})`}</dd>
        <dt>Peak</dt><dd className={`mono ${tone(pnl.peak_pct)}`}>{signed(pnl.peak_pct, "%")}</dd>
        <dt>Drawdown from peak</dt><dd className={`mono ${tone(pnl.drawdown_pct)}`}>{signed(pnl.drawdown_pct, "%")}</dd>
      </dl>
      <p className="form-hint">Basis: {pnl.basis}. Net = realized + unrealized, after entry fees.</p>
    </div>
  );
}
