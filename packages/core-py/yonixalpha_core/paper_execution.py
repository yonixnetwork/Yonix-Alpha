"""Simulated execution failures for PAPER trades.

Live orders fail: a transaction is dropped, not confirmed before its
blockhash expires, or rejected on chain (slippage). A paper book that always
fills looks better than live ever could, so paper applies a failure rate
to every entry and exit attempt:

- a failed ENTRY never opens the position (the candidate is rejected, as a
  failed live buy does);
- a failed EXIT leaves the position open: marks, trailing ratchet and stop
  tightening are still recorded, and the exit is attempted again on the next
  tick at *that* price, which is where the real cost of a failed exit shows
  up (a stop that fills later, lower).

Price drift (2026-10-09): a paper fill happens at the price the decision
saw; a live order lands seconds later, after other traders moved the price
(buys pay more, sells receive less). Every confirmed LIVE order records that
movement (execution_analysis: decision -> build -> landing for buys, expected
vs received for sells). With at least MIN_LIVE_SAMPLE measured orders of a
side, the median adverse movement is charged to every paper fill of that
side (charge_measured_live_drift); a favourable median is never credited.

Rates are never invented. The operator sets them (DB, dashboard; default
0%). Once at least MIN_LIVE_SAMPLE live orders of a side have a final
outcome, the rate measured from `execution_orders` is used instead (unless
switched off). Each attempt draws deterministically from a hash of
(position/assessment, attempt), so a replay of the same data gives the same
result and tests are exact.
"""

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import ExecutionOrder, PlatformSetting

SETTINGS_KEY = "paper_execution"
MIN_LIVE_SAMPLE = 20
MAX_FAILURE_PCT = Decimal(50)
MAX_DRIFT_PCT = Decimal(25)
DRIFT_SAMPLE = 200  # newest measured orders per side
FINAL_STATUSES = ("CONFIRMED", "FAILED", "EXPIRED")  # CANCELLED orders never reached the chain


@dataclass
class PaperExecutionSettings:
    entry_failure_pct: Decimal = Decimal(0)
    exit_failure_pct: Decimal = Decimal(0)
    use_measured_live_rates: bool = True
    # Solana paper trades pay what a LIVE round trip pays regardless of size
    # (live_trading.fixed_trade_costs: buy and sell network + priority fees,
    # rent reclaim or unreclaimed rent): counted in the risk plan exactly as
    # for LIVE and charged to the paper book. Off, paper sizes and books as
    # before (audit 2026-10-07: paper skipped them, so it took trades LIVE
    # refused and reported a better result than LIVE could get).
    charge_live_fixed_costs: bool = True
    # The price LIVE loses between decision and landing (module docstring).
    charge_measured_live_drift: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"entry_failure_pct": str(self.entry_failure_pct), "exit_failure_pct": str(self.exit_failure_pct),
                "use_measured_live_rates": self.use_measured_live_rates,
                "charge_live_fixed_costs": self.charge_live_fixed_costs,
                "charge_measured_live_drift": self.charge_measured_live_drift}


def parse_settings(data: dict[str, Any]) -> tuple[PaperExecutionSettings, list[str]]:
    s, errors = PaperExecutionSettings(), []
    for key, value in (data or {}).items():
        if key in ("use_measured_live_rates", "charge_live_fixed_costs", "charge_measured_live_drift"):
            if isinstance(value, bool):
                setattr(s, key, value)
            else:
                errors.append(f"{key}: must be true/false")
        elif key in ("entry_failure_pct", "exit_failure_pct"):
            try:
                d = Decimal(str(value))
            except Exception:  # noqa: BLE001
                errors.append(f"{key}: must be a number")
                continue
            if isinstance(value, bool) or not d.is_finite() or not Decimal(0) <= d <= MAX_FAILURE_PCT:
                errors.append(f"{key}: must be between 0 and {MAX_FAILURE_PCT}")
                continue
            setattr(s, key, d)
        else:
            errors.append(f"{key}: unknown setting")
    return s, errors


async def load_settings(session: AsyncSession) -> PaperExecutionSettings:
    row = await session.get(PlatformSetting, SETTINGS_KEY)
    s, errors = parse_settings((row.value if row else None) or {})
    return s if not errors else PaperExecutionSettings()


async def measured_live_rates(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """Failure rate of LIVE orders per side, from final outcomes only, counted
    once per position and side: the first final attempt decides. Retries of
    one stuck exit are not separate trials (2026-10-07: 6 450 retries of one
    position's sell, failing on the same program error, had pushed the
    measured sell failure rate, and with it every paper exit, to the 50 % cap).
    Orders without a position count individually."""
    from sqlalchemy import literal_column

    o = ExecutionOrder
    base = (o.mode == "LIVE", o.provider == "pumpportal_local", o.status.in_(FINAL_STATUSES), o.side.in_(("BUY", "SELL")))
    first = (select(o.side, o.status)
             .where(*base, o.position_id.is_not(None))
             .distinct(o.position_id, o.side)
             .order_by(o.position_id, o.side, o.created_at)).subquery()
    loose = select(o.side, o.status).where(*base, o.position_id.is_(None)).subquery()
    rows = []
    for sub in (first, loose):
        rows += (await session.execute(select(sub.c.side, sub.c.status, func.count(literal_column("*")))
                                       .group_by(sub.c.side, sub.c.status))).all()
    out: dict[str, dict[str, Any]] = {}
    for side in ("BUY", "SELL"):
        total = sum(n for sd, _, n in rows if sd == side)
        failed = sum(n for sd, st, n in rows if sd == side and st != "CONFIRMED")
        pct = (Decimal(failed) * 100 / total).quantize(Decimal("0.01")) if total else None
        out[side] = {"orders": total, "failed": failed, "failure_pct": pct, "usable": total >= MIN_LIVE_SAMPLE,
                     "counted": "first final attempt per position"}
    return out


async def effective_rates(session: AsyncSession) -> dict[str, Any]:
    """{'entry_pct', 'exit_pct', 'entry_source', 'exit_source', 'measured', 'settings'}"""
    s = await load_settings(session)
    measured = await measured_live_rates(session)
    out: dict[str, Any] = {"settings": s.to_dict(), "measured": measured}
    for kind, side, configured in (("entry", "BUY", s.entry_failure_pct), ("exit", "SELL", s.exit_failure_pct)):
        m = measured[side]
        if s.use_measured_live_rates and m["usable"]:
            out[f"{kind}_pct"] = min(m["failure_pct"], MAX_FAILURE_PCT)
            out[f"{kind}_source"] = f"measured from {m['orders']} live {side} orders"
        else:
            out[f"{kind}_pct"] = configured
            out[f"{kind}_source"] = "operator setting"
    return out


def _dec(v: Any) -> Decimal | None:
    try:
        d = Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None
    return d if d.is_finite() else None


def buy_drift_pct(diagnostics: dict[str, Any] | None) -> Decimal | None:
    """Market movement against a confirmed LIVE buy before it landed, in %:
    decision -> build, then build -> just before our trade (our own impact
    and the fees are not in it: paper charges those itself)."""
    comp = (((diagnostics or {}).get("price") or {}).get("components_pct")) or {}
    a, b = _dec(comp.get("decision_to_build_pct")), _dec(comp.get("build_to_landing_pct"))
    if a is None and b is None:
        return None
    return ((1 + (a or Decimal(0)) / 100) * (1 + (b or Decimal(0)) / 100) - 1) * 100


def sell_drift_pct(diagnostics: dict[str, Any] | None, amount: str | None, decimals: Any) -> Decimal | None:
    """How much less a confirmed LIVE sell received than its decision expected,
    in % (network fee added back: paper charges it separately)."""
    d = diagnostics or {}
    expected = _dec((d.get("decision") or {}).get("price_sol"))
    price = d.get("price") or {}
    all_in, fee = _dec(price.get("all_in_price_sol")), _dec(price.get("network_fee_sol")) or Decimal(0)
    raw, dec = _dec(amount), _dec(decimals)
    if not expected or expected <= 0 or all_in is None or raw is None or dec is None or raw <= 0:
        return None
    tokens = raw / (Decimal(10) ** int(dec))
    return (1 - (all_in + fee / tokens) / expected) * 100


def _median(values: list[Decimal]) -> Decimal:
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


async def measured_live_drift(session: AsyncSession) -> dict[str, Any]:
    """{'buy_pct', 'sell_pct', 'buy_n', 'sell_n', 'source'}: the median adverse
    price movement of the newest DRIFT_SAMPLE measured confirmed LIVE orders
    per side, clamped to [0, MAX_DRIFT_PCT]; None while fewer than
    MIN_LIVE_SAMPLE orders of that side are measured, or when switched off."""
    s = await load_settings(session)
    out: dict[str, Any] = {"buy_pct": None, "sell_pct": None, "buy_n": 0, "sell_n": 0,
                           "source": "switched off" if not s.charge_measured_live_drift else None}
    if not s.charge_measured_live_drift:
        return out
    o = ExecutionOrder
    rows = (await session.execute(select(o.side, o.diagnostics, o.amount, o.limits).where(
        o.mode == "LIVE", o.status == "CONFIRMED", o.side.in_(("BUY", "SELL")))
        .order_by(o.created_at.desc()).limit(DRIFT_SAMPLE * 4))).all()
    buys: list[Decimal] = []
    sells: list[Decimal] = []
    for side, diag, amount, limits in rows:
        if side == "BUY" and len(buys) < DRIFT_SAMPLE and (v := buy_drift_pct(diag)) is not None:
            buys.append(v)
        elif side == "SELL" and len(sells) < DRIFT_SAMPLE and \
                (v := sell_drift_pct(diag, amount, (limits or {}).get("decimals"))) is not None:
            sells.append(v)
    for key, values in (("buy", buys), ("sell", sells)):
        out[f"{key}_n"] = len(values)
        if len(values) >= MIN_LIVE_SAMPLE:
            out[f"{key}_pct"] = min(max(_median(values), Decimal(0)), MAX_DRIFT_PCT).quantize(Decimal("0.0001"))
    out["source"] = (f"median of {out['buy_n']} live buys / {out['sell_n']} live sells "
                     f"(at least {MIN_LIVE_SAMPLE} needed per side)")
    return out


def draw(key: str) -> Decimal:
    """Deterministic uniform value in [0, 1) for `key`."""
    h = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")
    return Decimal(h) / Decimal(2**64)


def simulated_failure(key: str, pct: Decimal) -> bool:
    return pct > 0 and draw(key) * 100 < pct
