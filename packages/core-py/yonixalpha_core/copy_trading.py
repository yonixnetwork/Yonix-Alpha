"""Copy trading (paper) on Solana, BSC and Robinhood Chain.

A copy target's trade is a CANDIDATE, never an order. "Smart wallet bought
→ buy" does not exist here: every copied buy passes the same controls and
checks as any other entry.
- Solana: the Solana gate must have approved the exact token recently.
- EVM: the launchpad's evidence-based status must allow paper, and the
  token must have a fresh safety PASS.
- Kill switch and trading controls (COPY TRADING, chain, NEW ENTRIES).
- Per-target limits: delay, chase guard, open positions.
- The shared risk planner (stop, take-profits, size limits).

Modes:
  NOTIFY    record + publish the signal, never trade
  BUY_ONLY  copy entries; exits come from our own risk plan
  MIRROR    copy entries and the target's sells: a partial sell is mirrored
            as the same fraction of the target's observed holding. On Solana
            a sell of at least `solana_full_exit_threshold` of the holding
            closes the copy; a smaller one is queued on the position
            (`queue_partial_exit`) and sold by the Solana position loop on
            its next pass, at that pass's price
  SELL_ONLY the target's buys are never copied; its sells are mirrored onto
            our OWN open PAPER positions in the same token (opened by our
            strategies), as the same fraction of the target's observed
            holding. With no observed buys of the target, the fraction is
            unknown and nothing is sold (TARGET_HOLDING_UNKNOWN). LIVE
            positions are never touched. The sell is queued on the position
            and filled by the service that owns it (data-evm / the Solana
            position loop), never by two services at once.

Idempotency: (target_id, source_event_id) is unique in copy_events; the
event row is inserted before anything is decided, so a restart can never
act on the same target trade twice.

Latency stages (ms): detection (target trade → seen), analysis (→ safety /
gate checked), risk (→ plan built), execution (→ paper fill), total. Paper
fills have no network landing; "landing" is reported as not applicable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

MODES = ("NOTIFY", "BUY_ONLY", "MIRROR", "SELL_ONLY")
SIZE_MODES = ("FIXED", "PROPORTIONAL")


@dataclass(frozen=True)
class CopySettings:
    size_mode: str = "FIXED"
    fixed_size: Decimal | None = None  # native units; None = the chain default
    proportional_pct: Decimal = Decimal("0.10")  # of the target's spend
    max_size: Decimal | None = None
    min_target_buy: Decimal = Decimal("0")  # ignore target buys below this (native units)
    max_open_positions: int = 2
    max_delay_seconds: int = 30
    chase_guard_pct: Decimal = Decimal("0.15")  # skip when our price is this much above the target's
    solana_full_exit_threshold: Decimal = Decimal("0.5")
    launchpads: tuple[str, ...] = ()  # empty = every launchpad of the chain

    def to_dict(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else list(v) if isinstance(v, tuple) else v)
                for k, v in asdict(self).items()}


DEFAULT_SIZE = {"solana": Decimal("0.05"), "bsc": Decimal("0.02"), "robinhood": Decimal("0.005")}
NATIVE = {"solana": "SOL", "bsc": "BNB", "robinhood": "ETH"}
DECIMALS_QUOTE = {"solana": 9, "bsc": 18, "robinhood": 18}


def parse_settings(data: dict[str, Any] | None) -> tuple[CopySettings, list[str]]:
    base, kw, errors = CopySettings(), {}, []
    for f in fields(CopySettings):
        if not data or f.name not in data or data[f.name] is None:
            continue
        v = data[f.name]
        try:
            if f.name == "size_mode":
                if str(v).upper() not in SIZE_MODES:
                    raise ValueError(f"must be one of {SIZE_MODES}")
                kw[f.name] = str(v).upper()
            elif f.name == "launchpads":
                kw[f.name] = tuple(str(x) for x in v)
            elif isinstance(getattr(base, f.name), int) and not isinstance(getattr(base, f.name), bool):
                if int(v) < 0:
                    raise ValueError("must not be negative")
                kw[f.name] = int(v)
            else:
                d = Decimal(str(v))
                if d < 0:
                    raise ValueError("must not be negative")
                kw[f.name] = d
        except (ValueError, TypeError, InvalidOperation) as exc:
            errors.append(f"{f.name}: {exc}")
    s = replace(base, **kw)
    if s.proportional_pct > 1:
        errors.append("proportional_pct must be at most 1")
    if s.solana_full_exit_threshold > 1:
        errors.append("solana_full_exit_threshold must be at most 1")
    return s, errors


def size_for(s: CopySettings, chain: str, target_quote: Decimal) -> Decimal:
    """Native-unit size of the copy (before the risk planner, which may
    size it down further)."""
    size = (target_quote * s.proportional_pct) if s.size_mode == "PROPORTIONAL" else (s.fixed_size or DEFAULT_SIZE[chain])
    if s.max_size is not None:
        size = min(size, s.max_size)
    return size


def chase_guard(target_price: Decimal | None, our_price: Decimal | None, pct: Decimal) -> str | None:
    """A blocker message when our entry price is more than `pct` above the
    price the target paid (we would be buying their exit liquidity)."""
    if target_price is None or our_price is None or target_price <= 0:
        return "target or our price unknown: cannot apply the chase guard"
    move = our_price / target_price - 1
    if move > pct:
        return f"our price is {move:.1%} above the target's ({pct:.0%} allowed)"
    return None


def observed_fraction(target_sold: Decimal, held_before: Decimal) -> Decimal | None:
    """SELL_ONLY: the share of its holding the target sold, or None when its
    holding was never observed (never assume a full exit)."""
    if held_before <= 0 or target_sold <= 0:
        return None
    return min(Decimal(1), target_sold / held_before)


def sell_fraction(target_sold: Decimal, target_held: Decimal) -> Decimal:
    if target_held <= 0:
        return Decimal(1)
    return min(Decimal(1), target_sold / target_held)


def latency(target_at: datetime, detected_at: datetime, analyzed_at: datetime | None = None,
            planned_at: datetime | None = None, executed_at: datetime | None = None) -> dict[str, Any]:
    def ms(a: datetime | None, b: datetime | None) -> int | None:
        return int((b - a).total_seconds() * 1000) if a is not None and b is not None else None

    last = executed_at or planned_at or analyzed_at or detected_at
    return {"detection": ms(target_at, detected_at), "analysis": ms(detected_at, analyzed_at),
            "risk": ms(analyzed_at, planned_at), "execution": ms(planned_at, executed_at),
            "landing": "not applicable (paper)", "total": ms(target_at, last)}


# --- Solana partial copy exits ----------------------------------------------------
# The Solana position loop (services/paper-trading gate_manage) owns pricing and
# fills for copy_solana positions, so a mirrored partial sell is queued on the
# position's plan and applied there as an extra exit, like an exit-intelligence
# REDUCE. Several partial sells before the next pass combine multiplicatively.
PARTIAL_EXIT_KEY = "copy_partial_exit"


def queue_partial_exit(plan: dict | None, fraction: Decimal, at: datetime) -> dict:
    """The plan with `fraction` of the remaining quantity queued for sale."""
    plan = dict(plan or {})
    pending = Decimal(str((plan.get(PARTIAL_EXIT_KEY) or {}).get("fraction") or 0))
    combined = 1 - (1 - pending) * (1 - min(max(fraction, Decimal(0)), Decimal(1)))
    plan[PARTIAL_EXIT_KEY] = {"fraction": str(combined.quantize(Decimal("0.000001"))), "requested_at": at.isoformat()}
    return plan


def pending_partial_exit(plan: dict | None) -> Decimal | None:
    raw = (plan or {}).get(PARTIAL_EXIT_KEY) or {}
    frac = Decimal(str(raw["fraction"])) if raw.get("fraction") is not None else None
    return frac if frac is not None and frac > 0 else None


def clear_partial_exit(plan: dict | None) -> dict:
    return {k: v for k, v in (plan or {}).items() if k != PARTIAL_EXIT_KEY}
