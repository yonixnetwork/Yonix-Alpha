"""Entry signals for the Solana engines.

These are transparent, rule-based heuristics. None has been backtested
against pump.fun history in this codebase, so none has a demonstrated edge
— they exist to decide *which* safety-cleared opportunities the paper
engine should take, so that labeled paper outcomes accumulate for later
evaluation and ML. The signal only ever gates entries further: the safety
gate decides whether a trade is allowed at all, and a qualified signal
cannot override any of its findings.
"""

from datetime import datetime
from decimal import Decimal

from yonixalpha_core.safety.models import StrategySignal, TradeFlow
from yonixalpha_core.solana.flow import Trade, acceleration, in_window

FRESH_NAME = "fresh_launch_flow"
FRESH_VERSION = "1"
MIGRATION_NAME = "post_migration_flow"
MIGRATION_VERSION = "1"
VALIDATION_NOTE = "unvalidated heuristic — no backtested edge; paper only"

MIN_BUY_SELL_VOLUME_RATIO = Decimal("1.5")
MIN_ACCELERATION = 1.0


def fresh_launch_signal(flow: TradeFlow | None, trades: list[Trade], now: datetime, decimals: int) -> StrategySignal:
    reasons: list[str] = []
    passed: list[str] = []
    if flow is None:
        return StrategySignal(FRESH_NAME, FRESH_VERSION, False, 0.0, ["no trade flow"])
    window = in_window(trades, now, flow.window_seconds)

    if flow.sell_volume_quote > 0:
        ratio = flow.buy_volume_quote / flow.sell_volume_quote
    else:
        ratio = Decimal("Infinity") if flow.buy_volume_quote > 0 else Decimal(0)
    (passed if ratio >= MIN_BUY_SELL_VOLUME_RATIO else reasons).append(
        f"buy/sell volume ratio {ratio:.2f} (need ≥ {MIN_BUY_SELL_VOLUME_RATIO})"
    )

    current, prior, accel = acceleration(trades, now, flow.window_seconds)
    accel_ok = current > 0 and (prior == 0 or (accel is not None and accel >= MIN_ACCELERATION))
    (passed if accel_ok else reasons).append(f"trades this window {current} vs previous {prior}")

    if len(window) >= 2:
        first, last = window[0].price(decimals), window[-1].price(decimals)
        change = (last / first - 1) if first > 0 else Decimal(0)
        (passed if change > 0 else reasons).append(f"price change over window {change:+.1%}")
    else:
        reasons.append("fewer than 2 trades in window — no price trend")

    qualified = not reasons
    strength = len(passed) / (len(passed) + len(reasons))
    return StrategySignal(FRESH_NAME, FRESH_VERSION, qualified, strength,
                          (passed if qualified else reasons) + [VALIDATION_NOTE])


def post_migration_signal(buys_h1: int | None, sells_h1: int | None, age_seconds: float | None) -> StrategySignal:
    reasons: list[str] = []
    if buys_h1 is None or sells_h1 is None:
        return StrategySignal(MIGRATION_NAME, MIGRATION_VERSION, False, 0.0, ["pool transaction counts unavailable"])
    if buys_h1 <= sells_h1:
        reasons.append(f"h1 buys {buys_h1} ≤ sells {sells_h1}")
    if age_seconds is None or age_seconds < 120:
        reasons.append("pool younger than 2 minutes — waiting for price discovery")
    qualified = not reasons
    return StrategySignal(MIGRATION_NAME, MIGRATION_VERSION, qualified, 1.0 if qualified else 0.3,
                          (reasons or [f"h1 buys {buys_h1} > sells {sells_h1}"]) + [VALIDATION_NOTE])
