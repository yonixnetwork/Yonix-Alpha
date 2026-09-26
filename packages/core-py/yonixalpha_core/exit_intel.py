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
EXIT_NOW (one signal is enough; risk safety over profit targets):
- liquidity collapse: real liquidity down >= exit_emergency_liquidity_drop;
- price collapse: price down >= exit_emergency_price_drop from the highest
  price since entry;
- creator dump: the creator's holding fell by half or more AND the creator
  sold in the window.

Also REDUCE: volume collapse (latest window volume <= exit_volume_collapse_
ratio of the previous window) together with buyers stopping or sellers
dominating.

The thresholds are transparent heuristics, not fitted on data, and every one
is a dashboard setting (SafetySettings.exit_*); the defaults are the values
this module used before they became settings.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from yonixalpha_core.solana.flow import Trade, in_window

WINDOW_SECONDS = 120
LIQUIDITY_DROP_EXIT = Decimal("0.40")
LIQUIDITY_DROP_WARN = Decimal("0.20")
REDUCE_FRACTION = Decimal("0.5")
CREATOR_MIN_SHARE = Decimal("0.02")
DISTRIBUTION_DROP = Decimal("0.15")
CONCENTRATION_RISE = Decimal("0.15")
VERSION = "3"


@dataclass
class ExitDecision:
    action: str  # HOLD | REDUCE | EXIT | EXIT_NOW
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


@dataclass
class ExitConfig:
    liquidity_drop_exit: Decimal = LIQUIDITY_DROP_EXIT
    liquidity_drop_warn: Decimal = LIQUIDITY_DROP_WARN
    reduce_fraction: Decimal = REDUCE_FRACTION
    volume_collapse_ratio: Decimal = Decimal("0.25")
    emergency_liquidity_drop: Decimal = Decimal("0.60")
    emergency_price_drop: Decimal = Decimal("0.35")

    @classmethod
    def from_settings(cls, s) -> "ExitConfig":
        return cls(liquidity_drop_exit=s.exit_liquidity_drop_exit, liquidity_drop_warn=s.exit_liquidity_drop_warn,
                   reduce_fraction=s.exit_reduce_fraction, volume_collapse_ratio=s.exit_volume_collapse_ratio,
                   emergency_liquidity_drop=s.exit_emergency_liquidity_drop, emergency_price_drop=s.exit_emergency_price_drop)


def solana_exit_decision(trades: list[Trade], now: datetime, creator: str | None,
                         entry_liquidity: Decimal | None, current_liquidity: Decimal | None,
                         holders_entry: dict | None = None, holders_now: dict | None = None,
                         cfg: ExitConfig | None = None, highest_price: Decimal | None = None,
                         current_price: Decimal | None = None) -> ExitDecision:
    cfg = cfg or ExitConfig()
    w = in_window(trades, now, WINDOW_SECONDS)
    prev = [t for t in trades if now - timedelta(seconds=2 * WINDOW_SECONDS) < t.at <= now - timedelta(seconds=WINDOW_SECONDS)]
    buy_vol = sum(t.sol_lamports for t in w if t.is_buy)
    sell_vol = sum(t.sol_lamports for t in w if not t.is_buy)
    prev_vol = sum(t.sol_lamports for t in prev)
    buyers = len({t.trader for t in w if t.is_buy})
    prev_buyers = len({t.trader for t in prev if t.is_buy})
    sellers = len({t.trader for t in w if not t.is_buy})
    creator_sold = bool(creator) and any(t.trader == creator and not t.is_buy for t in w)
    drop = None
    if entry_liquidity and current_liquidity is not None and entry_liquidity > 0:
        drop = 1 - current_liquidity / entry_liquidity
    price_drop = None
    if highest_price and current_price is not None and highest_price > 0:
        price_drop = max(Decimal(0), 1 - current_price / highest_price)
    sell_pressure = sell_vol > 0 and sell_vol >= 2 * buy_vol
    seller_dominance = sellers >= 2 * max(buyers, 1)
    volume_collapse = prev_vol > 0 and (buy_vol + sell_vol) <= prev_vol * cfg.volume_collapse_ratio
    buyers_stopped = prev_buyers >= 3 and buyers == 0
    holder = holder_changes(holders_entry, holders_now)
    creator_dump = any(h.startswith("creator holdings fell") for h in holder) and creator_sold
    metrics = {"buy_volume": buy_vol, "sell_volume": sell_vol, "buyers": buyers, "sellers": sellers,
               "previous_window_volume": prev_vol, "previous_window_buyers": prev_buyers,
               "creator_sold": creator_sold, "liquidity_drop": str(drop) if drop is not None else None,
               "price_drop_from_high": str(price_drop) if price_drop is not None else None,
               "volume_collapse": volume_collapse, "buyers_stopped": buyers_stopped,
               "holder_changes": holder, "holders_checked": holders_now is not None}

    # Emergencies: one signal is enough.
    if drop is not None and drop >= cfg.emergency_liquidity_drop:
        return ExitDecision("EXIT_NOW", [f"EMERGENCY: liquidity collapsed {drop:.0%} since entry"], Decimal(1), metrics)
    if price_drop is not None and price_drop >= cfg.emergency_price_drop:
        return ExitDecision("EXIT_NOW", [f"EMERGENCY: price {price_drop:.0%} below the high since entry"], Decimal(1), metrics)
    if creator_dump:
        return ExitDecision("EXIT_NOW", ["EMERGENCY: creator dumping (holding halved and selling now)"], Decimal(1), metrics)

    if drop is not None and drop >= cfg.liquidity_drop_exit and sell_pressure:
        return ExitDecision("EXIT", [f"liquidity down {drop:.0%} since entry with sellers dominating volume"], Decimal(1), metrics)
    if creator_sold and sell_pressure:
        return ExitDecision("EXIT", ["creator sold while sellers dominate volume"], Decimal(1), metrics)
    if holder and sell_pressure:
        return ExitDecision("EXIT", holder + ["sellers dominate volume"], Decimal(1), metrics)
    if holder and (seller_dominance or (drop is not None and drop >= cfg.liquidity_drop_warn)):
        why = "sellers outnumber buyers 2:1" if seller_dominance else f"liquidity down {drop:.0%} since entry"
        return ExitDecision("REDUCE", holder + [why], cfg.reduce_fraction, metrics)
    if sell_pressure and seller_dominance:
        return ExitDecision("REDUCE", [f"sell volume {sell_vol} vs buy {buy_vol}; {sellers} sellers vs {buyers} buyers"],
                            cfg.reduce_fraction, metrics)
    if volume_collapse and (buyers_stopped or seller_dominance):
        why = "no new buyers" if buyers_stopped else "sellers outnumber buyers 2:1"
        return ExitDecision("REDUCE", [f"volume collapsed to {(buy_vol + sell_vol) / prev_vol:.0%} of the previous "
                                       f"{WINDOW_SECONDS}s window", why], cfg.reduce_fraction, metrics)
    return ExitDecision("HOLD", [], Decimal(0), metrics)
