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

Holder changes since entry (re-read from chain about once a minute) are one
more signal, never sufficient alone:
- CREATOR DUMP: the creator held >= 2% at entry and now holds <= half of it;
- DISTRIBUTION: the top-10 holders' share fell >= 15 points (insiders
  handing supply to the market);
- CONCENTRATION: the largest holder's share rose >= 15 points.
A holder change with sell pressure is EXIT; with seller dominance or a
>= 20% liquidity drop it is REDUCE; alone it is HOLD (recorded in metrics).
The thresholds are transparent heuristics, not fitted on data.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from yonixalpha_core.solana.flow import Trade, in_window

WINDOW_SECONDS = 120
LIQUIDITY_DROP_EXIT = Decimal("0.40")
LIQUIDITY_DROP_WARN = Decimal("0.20")
REDUCE_FRACTION = Decimal("0.5")
CREATOR_MIN_SHARE = Decimal("0.02")
DISTRIBUTION_DROP = Decimal("0.15")
CONCENTRATION_RISE = Decimal("0.15")
VERSION = "2"


@dataclass
class ExitDecision:
    action: str  # HOLD | REDUCE | EXIT
    reasons: list[str] = field(default_factory=list)
    fraction: Decimal = Decimal(0)
    metrics: dict = field(default_factory=dict)


def holder_changes(entry: dict | None, current: dict | None) -> list[str]:
    """Adverse holder-distribution changes since entry. `entry`/`current`
    hold top1_share, top10_share, creator_share (strings or Decimals)."""
    if not entry or not current:
        return []

    def d(src, key):
        v = src.get(key)
        return Decimal(str(v)) if v not in (None, "") else None

    out = []
    c0, c1 = d(entry, "creator_share"), d(current, "creator_share")
    if c0 is not None and c1 is not None and c0 >= CREATOR_MIN_SHARE and c1 <= c0 / 2:
        out.append(f"creator holdings fell from {c0:.1%} to {c1:.1%} of supply")
    t0, t1 = d(entry, "top10_share"), d(current, "top10_share")
    if t0 is not None and t1 is not None and t0 - t1 >= DISTRIBUTION_DROP:
        out.append(f"top-10 holders' share fell from {t0:.1%} to {t1:.1%} (distribution)")
    o0, o1 = d(entry, "top1_share"), d(current, "top1_share")
    if o0 is not None and o1 is not None and o1 - o0 >= CONCENTRATION_RISE:
        out.append(f"largest holder's share rose from {o0:.1%} to {o1:.1%}")
    return out


def solana_exit_decision(trades: list[Trade], now: datetime, creator: str | None,
                         entry_liquidity: Decimal | None, current_liquidity: Decimal | None,
                         holders_entry: dict | None = None, holders_now: dict | None = None) -> ExitDecision:
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
    holder = holder_changes(holders_entry, holders_now)
    metrics = {"buy_volume": buy_vol, "sell_volume": sell_vol, "buyers": buyers, "sellers": sellers,
               "creator_sold": creator_sold, "liquidity_drop": str(drop) if drop is not None else None,
               "holder_changes": holder, "holders_checked": holders_now is not None}

    if drop is not None and drop >= LIQUIDITY_DROP_EXIT and sell_pressure:
        return ExitDecision("EXIT", [f"liquidity down {drop:.0%} since entry with sellers dominating volume"], Decimal(1), metrics)
    if creator_sold and sell_pressure:
        return ExitDecision("EXIT", ["creator sold while sellers dominate volume"], Decimal(1), metrics)
    if holder and sell_pressure:
        return ExitDecision("EXIT", holder + ["sellers dominate volume"], Decimal(1), metrics)
    if holder and (seller_dominance or (drop is not None and drop >= LIQUIDITY_DROP_WARN)):
        why = "sellers outnumber buyers 2:1" if seller_dominance else f"liquidity down {drop:.0%} since entry"
        return ExitDecision("REDUCE", holder + [why], REDUCE_FRACTION, metrics)
    if sell_pressure and seller_dominance:
        return ExitDecision("REDUCE", [f"sell volume {sell_vol} vs buy {buy_vol}; {sellers} sellers vs {buyers} buyers"],
                            REDUCE_FRACTION, metrics)
    return ExitDecision("HOLD", [], Decimal(0), metrics)
