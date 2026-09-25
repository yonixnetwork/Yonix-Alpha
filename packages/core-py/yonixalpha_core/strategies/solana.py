"""Entry signals for the Solana engines.

These are transparent, rule-based heuristics. None has been backtested
against pump.fun history in this codebase, so none has a demonstrated edge
— they exist to decide *which* safety-cleared opportunities the paper
engine should take, so that labeled paper outcomes accumulate for later
evaluation and ML. The signal only ever gates entries further: the safety
gate decides whether a trade is allowed at all, and a qualified signal
cannot override any of its findings.
"""

from datetime import datetime, timedelta
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


MOMENTUM_NAME = "solana_momentum"
MOMENTUM_VERSION = "1"
MOMENTUM_MIN_AGE_SECONDS = 30 * 60


def _window_stats(window: list[Trade], decimals: int) -> dict:
    buys = [t for t in window if t.is_buy]
    sells = [t for t in window if not t.is_buy]
    vol = sum(t.sol_lamports for t in window)
    return {
        "trades": len(window),
        "volume": vol,
        "buy_volume": sum(t.sol_lamports for t in buys),
        "sell_volume": sum(t.sol_lamports for t in sells),
        "buyers": len({t.trader for t in buys}),
        "sellers": len({t.trader for t in sells}),
        "first_price": window[0].price(decimals) if window else None,
        "last_price": window[-1].price(decimals) if window else None,
        "real_sol_last": window[-1].virtual_sol if window else None,
    }


def _ratio(a: float, b: float) -> float | None:
    return a / b if b else (float("inf") if a else None)


def momentum_signal(trades: list[Trade], now: datetime, window_seconds: int, decimals: int) -> StrategySignal:
    """Established-token momentum (spec §12). Compares the current window
    with the previous one on every axis at once; any single axis failing
    keeps it unqualified. High volume alone never qualifies."""
    cur = _window_stats(in_window(trades, now, window_seconds), decimals)
    prev = _window_stats(in_window(trades, now - timedelta(seconds=window_seconds), window_seconds), decimals)
    reasons_ok: list[str] = []
    reasons_no: list[str] = []

    def check(ok: bool, text: str) -> None:
        (reasons_ok if ok else reasons_no).append(text)

    if cur["trades"] < 2 or prev["trades"] < 2:
        return StrategySignal(MOMENTUM_NAME, MOMENTUM_VERSION, False, 0.0,
                              ["fewer than 2 trades in the current or previous window — no baseline", VALIDATION_NOTE])
    price_now = (cur["last_price"] / cur["first_price"] - 1) if cur["first_price"] else Decimal(0)
    price_prev = (prev["last_price"] / prev["first_price"] - 1) if prev["first_price"] else Decimal(0)
    vol_acc = _ratio(cur["volume"], prev["volume"])
    tx_acc = _ratio(cur["trades"], prev["trades"])
    buyer_acc = _ratio(cur["buyers"], prev["buyers"])
    seller_acc = _ratio(cur["sellers"], prev["sellers"])
    imbalance = _ratio(cur["buy_volume"], cur["sell_volume"])

    check(price_now > 0 and price_now > price_prev, f"price change {price_now:+.1%} vs previous {price_prev:+.1%}")
    check(vol_acc is not None and vol_acc >= 1.5, f"volume acceleration {vol_acc if vol_acc is None else round(vol_acc, 2)}x (need ≥ 1.5)")
    check(tx_acc is not None and tx_acc >= 1.5, f"transaction acceleration {tx_acc if tx_acc is None else round(tx_acc, 2)}x (need ≥ 1.5)")
    check(buyer_acc is not None and buyer_acc >= 1.3, f"unique-buyer acceleration {buyer_acc if buyer_acc is None else round(buyer_acc, 2)}x (need ≥ 1.3)")
    check(seller_acc is None or buyer_acc is None or buyer_acc >= seller_acc,
          f"buyers growing at least as fast as sellers ({buyer_acc} vs {seller_acc})")
    check(imbalance is not None and imbalance >= 1.3, f"buy/sell volume imbalance {imbalance if imbalance is None else round(imbalance, 2)} (need ≥ 1.3)")
    qualified = not reasons_no
    strength = len(reasons_ok) / (len(reasons_ok) + len(reasons_no))
    return StrategySignal(MOMENTUM_NAME, MOMENTUM_VERSION, qualified, strength,
                          (reasons_ok if qualified else reasons_no) + [VALIDATION_NOTE])
