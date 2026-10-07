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

    def to_dict(self) -> dict[str, Any]:
        return {"entry_failure_pct": str(self.entry_failure_pct), "exit_failure_pct": str(self.exit_failure_pct),
                "use_measured_live_rates": self.use_measured_live_rates,
                "charge_live_fixed_costs": self.charge_live_fixed_costs}


def parse_settings(data: dict[str, Any]) -> tuple[PaperExecutionSettings, list[str]]:
    s, errors = PaperExecutionSettings(), []
    for key, value in (data or {}).items():
        if key in ("use_measured_live_rates", "charge_live_fixed_costs"):
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


def draw(key: str) -> Decimal:
    """Deterministic uniform value in [0, 1) for `key`."""
    h = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")
    return Decimal(h) / Decimal(2**64)


def simulated_failure(key: str, pct: Decimal) -> bool:
    return pct > 0 and draw(key) * 100 < pct
