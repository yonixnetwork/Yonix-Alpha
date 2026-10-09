"""Sellable-amount protection for partial exits (2026-10-10).

A take-profit sells a fraction of the ORIGINAL quantity (paper_engine
manage_step: `initial × fraction`, capped at what remains). Two things can go
wrong with that alone:

  - the sale leaves a remainder too small to ever be worth selling (a few raw
    units after rounding, or worth less than the network fee of the sell
    that would take it out);
  - the partial sale itself returns less than it costs (each sell pays its
    own network + priority fee).

`check_exit` looks at ONE proposed sale against the whole position and
returns what to do, in integer raw token units:

  - a remainder at or below DUST_RAW, or worth less than
    `min_remainder_fee_multiple` sell fees, is folded into this sale
    (MERGED_REMAINDER);
  - a partial sale worth less than `min_sale_fee_multiple` sell fees is not
    sent; its quantity stays in the position and leaves with the next exit
    (DEFERRED);
  - a full exit (stop loss, trailing stop, manual / emergency exit) is NEVER
    reduced, deferred or blocked here: protection only ever changes partial
    take-profits.

Nothing here buys, burns or transfers tokens, and nothing sells more than the
position holds. A price of None means the value checks cannot be made: only
the raw-unit dust rule applies and the result says NOT_VERIFIED.

`plan_schedule` lays the remaining take-profits out over the whole position
so the operator can see, before TP1, what each level will sell and what the
trailing stop is left with.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from decimal import ROUND_DOWN, Decimal
from typing import Any

# exit states
SELLABLE = "SELLABLE"
BELOW_ROUTE_MINIMUM = "BELOW_ROUTE_MINIMUM"
NO_ROUTE = "NO_ROUTE"
BALANCE_MISMATCH = "BALANCE_MISMATCH"
TRANSFER_RESTRICTION = "TRANSFER_RESTRICTION"
DUST_UNSELLABLE = "DUST_UNSELLABLE"
EXIT_BLOCKED = "EXIT_BLOCKED"
RETRYABLE = "RETRYABLE"
STATES = (SELLABLE, BELOW_ROUTE_MINIMUM, NO_ROUTE, BALANCE_MISMATCH, TRANSFER_RESTRICTION, DUST_UNSELLABLE,
          EXIT_BLOCKED, RETRYABLE)

# actions
SELL, MERGED_REMAINDER, DEFERRED, NOTHING = "SELL", "MERGED_REMAINDER", "DEFERRED", "NOTHING"

DUST_RAW = 1  # live_trading.DUST_RAW: at or below this many raw units a position is empty
# Partial sales protection may defer (profit-taking) or only enlarge (defensive reduce).
DEFERRABLE_PARTIALS = ("copy_partial_sell",)  # plus every take_profit_N
DEFENSIVE_PARTIALS = ("exit_intel_reduce",)

SETTINGS_KEY = "exit_protection_settings"
MODES = ("OFF", "PAPER", "PAPER_AND_LIVE")


@dataclass
class ExitProtectionSettings:
    # Rollout (spec §24 F/G): PAPER changes paper exits only; LIVE exits are
    # checked and the would-be change is recorded, not applied, until the
    # operator chooses PAPER_AND_LIVE.
    mode: str = "PAPER"
    min_sale_fee_multiple: Decimal = Decimal("3")  # a partial sale must return >= 3 sell fees
    min_remainder_fee_multiple: Decimal = Decimal("3")  # a remainder must be worth >= 3 sell fees

    def to_dict(self) -> dict[str, Any]:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}

    def applies(self, live: bool) -> bool:
        return self.mode == "PAPER_AND_LIVE" if live else self.mode in ("PAPER", "PAPER_AND_LIVE")


def parse_settings(raw: dict[str, Any] | None) -> tuple[ExitProtectionSettings, list[str]]:
    s, errors = ExitProtectionSettings(), []
    for f in fields(ExitProtectionSettings):
        if not raw or f.name not in raw:
            continue
        v = raw[f.name]
        if f.name == "mode":
            if v not in MODES:
                errors.append(f"mode: one of {', '.join(MODES)}")
            else:
                s.mode = v
            continue
        try:
            d = Decimal(str(v))
        except ArithmeticError:
            errors.append(f"{f.name}: a number")
            continue
        if not Decimal(0) <= d <= Decimal(100):
            errors.append(f"{f.name}: 0..100")
        else:
            setattr(s, f.name, d)
    return s, errors


async def load_settings(session) -> ExitProtectionSettings:
    from yonixalpha_core.db.models import PlatformSetting

    row = await session.get(PlatformSetting, SETTINGS_KEY)
    s, errors = parse_settings(row.value if row else None)
    return ExitProtectionSettings() if errors else s


def to_raw(quantity: Decimal, decimals: int) -> int:
    """Whole tokens -> integer raw units, never rounded up (no oversell)."""
    return int((Decimal(quantity) * Decimal(10) ** decimals).to_integral_value(ROUND_DOWN))


def from_raw(raw: int, decimals: int) -> Decimal:
    return Decimal(raw) / Decimal(10) ** decimals


@dataclass
class ExitCheck:
    state: str
    action: str
    proposed_raw: int
    sell_raw: int
    remainder_raw: int
    reason: str
    sale_value_sol: Decimal | None = None
    remainder_value_sol: Decimal | None = None
    sell_fee_sol: Decimal | None = None
    verified: bool = True  # False when no price: value checks not made
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}

    @property
    def changed(self) -> bool:
        return self.action in (MERGED_REMAINDER, DEFERRED)


def is_full_exit(reason: str) -> bool:
    """Everything but a take-profit level, a defensive reduce and a mirrored
    partial copy sell sells the whole position (stop loss, trailing stop,
    manual / emergency exit, exit intelligence EXIT, ...)."""
    return not (reason.startswith("take_profit_") or reason in DEFERRABLE_PARTIALS or reason in DEFENSIVE_PARTIALS)


def may_defer(reason: str) -> bool:
    return reason.startswith("take_profit_") or reason in DEFERRABLE_PARTIALS


def check_exit(remaining_raw: int, proposed_raw: int, reason: str, price_per_raw_sol: Decimal | None,
               sell_fee_sol: Decimal, settings: ExitProtectionSettings | None = None, *,
               route_available: bool = True, transfer_restricted: bool = False) -> ExitCheck:
    """What to sell now for one proposed sale (see the module docstring)."""
    s = settings or ExitProtectionSettings()
    full = is_full_exit(reason)
    if remaining_raw <= 0:
        return ExitCheck(BALANCE_MISMATCH, NOTHING, proposed_raw, 0, 0, "the position holds no tokens to sell")
    if transfer_restricted:
        return ExitCheck(TRANSFER_RESTRICTION, NOTHING, proposed_raw, 0, remaining_raw,
                         "the token restricts transfers: kept for reconciliation, not sold")
    if not route_available:
        return ExitCheck(NO_ROUTE, NOTHING, proposed_raw, 0, remaining_raw,
                         "no executable route right now: kept and monitored")
    sell = remaining_raw if full else max(0, min(proposed_raw, remaining_raw))
    if sell <= 0:
        return ExitCheck(BELOW_ROUTE_MINIMUM, NOTHING, proposed_raw, 0, remaining_raw, "nothing to sell")
    remainder = remaining_raw - sell
    priced = price_per_raw_sol is not None and price_per_raw_sol > 0
    value = (lambda raw: Decimal(raw) * price_per_raw_sol) if priced else (lambda raw: None)
    check = ExitCheck(SELLABLE, SELL, proposed_raw, sell, remainder, "sold as planned",
                      sale_value_sol=value(sell), remainder_value_sol=value(remainder), sell_fee_sol=sell_fee_sol,
                      verified=priced)
    if full:
        if priced and check.sale_value_sol < sell_fee_sol:
            check.notes.append("proceeds below the sell fee; still sold: a protective exit is never held back, and "
                               "closing the token account returns its rent")
        return check
    if not priced:
        check.notes.append("no price: value checks not made (NOT_VERIFIED); only the raw dust rule applied")
    min_sale = s.min_sale_fee_multiple * sell_fee_sol
    if priced and check.sale_value_sol < min_sale and may_defer(reason) and remainder > 0:
        # (a sale that empties the position is never deferred: it closes the account and returns its rent)
        # Too small to be worth its own transaction: keep it; it leaves with the next exit.
        return ExitCheck(BELOW_ROUTE_MINIMUM, DEFERRED, proposed_raw, 0, remaining_raw,
                         f"partial sale worth {check.sale_value_sol:.9f} SOL is below {s.min_sale_fee_multiple}x the "
                         f"{sell_fee_sol} SOL sell fee: deferred to the next exit",
                         sale_value_sol=check.sale_value_sol, remainder_value_sol=value(remaining_raw),
                         sell_fee_sol=sell_fee_sol, verified=True)
    if 0 < remainder <= DUST_RAW:
        why = f"the {remainder} raw unit(s) left would be dust"
    elif priced and remainder > 0 and check.remainder_value_sol < s.min_remainder_fee_multiple * sell_fee_sol:
        why = (f"the remainder worth {check.remainder_value_sol:.9f} SOL is below {s.min_remainder_fee_multiple}x the "
               f"{sell_fee_sol} SOL sell fee")
    else:
        return check
    return ExitCheck(SELLABLE, MERGED_REMAINDER, proposed_raw, remaining_raw, 0, why + ": folded into this sale",
                     sale_value_sol=value(remaining_raw), remainder_value_sol=value(0) if priced else None,
                     sell_fee_sol=sell_fee_sol, verified=priced)


def plan_schedule(initial_raw: int, remaining_raw: int, take_profits: list[tuple[Decimal, Decimal]], hits: list[int],
                  price_per_raw_sol: Decimal | None, sell_fee_sol: Decimal,
                  settings: ExitProtectionSettings | None = None) -> dict[str, Any]:
    """The remaining take-profits laid over the whole position. Fractions are
    of the ORIGINAL quantity (manage_step's basis); each level sells
    min(initial x fraction, what is left). The trailing stop / stop loss takes
    what remains after the last level. Read-only: nothing is sold."""
    s = settings or ExitProtectionSettings()
    left = remaining_raw
    levels = []
    for i, (tp_price, fraction) in enumerate(take_profits):
        if i in hits:
            levels.append({"level": i + 1, "price": str(tp_price), "fraction_of_original": str(fraction), "status": "HIT"})
            continue
        proposed = to_raw_fraction(initial_raw, fraction)
        c = check_exit(left, proposed, f"take_profit_{i + 1}", price_per_raw_sol, sell_fee_sol, s)
        levels.append({"level": i + 1, "price": str(tp_price), "fraction_of_original": str(fraction),
                       "status": "PLANNED", "sell_raw": c.sell_raw, "action": c.action, "state": c.state,
                       "reason": c.reason})
        left -= c.sell_raw
    runner = {"raw": left}
    if price_per_raw_sol:
        runner["value_sol"] = str(Decimal(left) * price_per_raw_sol)
    return {"basis": "fraction of the ORIGINAL quantity; each level capped at what remains",
            "initial_raw": initial_raw, "remaining_raw": remaining_raw, "levels": levels,
            "left_for_trailing_or_stop": runner, "mode": s.mode}


def to_raw_fraction(initial_raw: int, fraction: Decimal) -> int:
    return int((Decimal(initial_raw) * Decimal(fraction)).to_integral_value(ROUND_DOWN))


def position_view(p: Any, settings: ExitProtectionSettings, sell_fee_sol: Decimal) -> dict[str, Any]:
    """Exit plan of one open position for the dashboard: original / current
    quantities in raw and token units, what each remaining take-profit will
    sell, what the trailing stop / stop loss is left with. Read-only."""
    venue = (getattr(p, "plan", None) or {}).get("venue") or {}
    decimals = venue.get("decimals")
    out: dict[str, Any] = {"position_id": str(p.id), "status": p.status, "decimals": decimals,
                           "route": getattr(p, "execution_route", None) or venue.get("type"),
                           "exit_failures": getattr(p, "exit_failures", 0), "settings": settings.to_dict(),
                           "sell_fee_sol": str(sell_fee_sol)}
    if decimals is None:
        out["state"] = "NOT_VERIFIED"
        out["note"] = "token decimals unknown: raw amounts cannot be planned"
        return out
    dec = int(decimals)
    initial = getattr(p, "initial_quantity", None) or p.quantity or Decimal(0)
    remaining = p.remaining_quantity if p.remaining_quantity is not None else p.quantity
    initial_raw, remaining_raw = to_raw(initial, dec), to_raw(remaining or Decimal(0), dec)
    price = getattr(p, "last_price", None)
    per_raw = (Decimal(price) / Decimal(10) ** dec) if price else None
    tps = [(Decimal(tp["price"]["value"]), Decimal(tp["exit_fraction"])) for tp in (p.plan or {}).get("take_profits", [])]
    out.update({"initial_raw": initial_raw, "initial_tokens": str(from_raw(initial_raw, dec)),
                "remaining_raw": remaining_raw, "remaining_tokens": str(from_raw(remaining_raw, dec)),
                "sold_raw": max(0, initial_raw - remaining_raw), "price_sol": str(price) if price else None,
                "price_at": getattr(p, "last_marked_at", None).isoformat() if getattr(p, "last_marked_at", None) else None,
                "schedule": plan_schedule(initial_raw, remaining_raw, tps, list(p.tp_hits or []), per_raw, sell_fee_sol,
                                          settings)})
    if remaining_raw <= DUST_RAW and p.status == "open":
        out["state"] = DUST_UNSELLABLE
    elif per_raw is not None and remaining_raw and Decimal(remaining_raw) * per_raw < sell_fee_sol:
        out["state"] = DUST_UNSELLABLE
        out["note"] = "what remains is worth less than one sell fee; a full exit still sells it (and returns the rent)"
    else:
        out["state"] = SELLABLE if per_raw is not None else "NOT_VERIFIED"
    out["note"] = out.get("note") or ("estimates from the last marked price; the route and quote are checked again "
                                      "when a sell is built, and a quote never guarantees landing")
    return out
