"""Exit intelligence for open Solana curve positions (spec §60): continuous
evaluation of flow, liquidity and creator behaviour on top of the plan's
stop / take-profits / trailing stop, which always stay in force.

No single metric triggers anything (declining volume alone is never a sell
signal). Every trigger needs two independent pieces of evidence:
- EXIT:   real liquidity fell >= 40% since entry AND sellers dominate volume,
          or the creator sold in the window AND sellers dominate volume;
- REDUCE: sell volume >= 2x buy volume AND unique sellers >= 2x unique
          buyers (sell half of what remains, at most once per 5 minutes);
- otherwise HOLD.
The thresholds are transparent heuristics, not fitted on data.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from yonixalpha_core.solana.flow import Trade, in_window

WINDOW_SECONDS = 120
LIQUIDITY_DROP_EXIT = Decimal("0.40")
REDUCE_FRACTION = Decimal("0.5")
VERSION = "1"


@dataclass
class ExitDecision:
    action: str  # HOLD | REDUCE | EXIT
    reasons: list[str] = field(default_factory=list)
    fraction: Decimal = Decimal(0)
    metrics: dict = field(default_factory=dict)


def solana_exit_decision(trades: list[Trade], now: datetime, creator: str | None,
                         entry_liquidity: Decimal | None, current_liquidity: Decimal | None) -> ExitDecision:
    w = in_window(trades, now, WINDOW_SECONDS)
    buy_vol = sum(t.sol_lamports for t in w if t.is_buy)
    sell_vol = sum(t.sol_lamports for t in w if not t.is_buy)
    buyers = len({t.trader for t in w if t.is_buy})
    sellers = len({t.trader for t in w if not t.is_buy})
    creator_sold = bool(creator) and any(t.trader == creator and not t.is_buy for t in w)
    drop = None
    if entry_liquidity and current_liquidity is not None and entry_liquidity > 0:
        drop = 1 - current_liquidity / entry_liquidity
    sell_pressure = sell_vol > 0 and sell_vol >= 2 * buy_vol
    seller_dominance = sellers >= 2 * max(buyers, 1)
    metrics = {"buy_volume": buy_vol, "sell_volume": sell_vol, "buyers": buyers, "sellers": sellers,
               "creator_sold": creator_sold, "liquidity_drop": str(drop) if drop is not None else None}

    if drop is not None and drop >= LIQUIDITY_DROP_EXIT and sell_pressure:
        return ExitDecision("EXIT", [f"liquidity down {drop:.0%} since entry with sellers dominating volume"], Decimal(1), metrics)
    if creator_sold and sell_pressure:
        return ExitDecision("EXIT", ["creator sold while sellers dominate volume"], Decimal(1), metrics)
    if sell_pressure and seller_dominance:
        return ExitDecision("REDUCE", [f"sell volume {sell_vol} vs buy {buy_vol}; {sellers} sellers vs {buyers} buyers"],
                            REDUCE_FRACTION, metrics)
    return ExitDecision("HOLD", [], Decimal(0), metrics)
