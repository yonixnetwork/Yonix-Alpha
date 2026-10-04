"""SELL / HOLD decision points of open EVM paper positions (master §41, the
exit half; the entry half is ml.evm_samples).

While a position is open, data-evm records a checkpoint every
SAMPLE_EVERY and on every tick that sold something:

  features   only what was known at that moment: time held, unrealized and
             peak return, drawdown from the peak, distance to the stop,
             remaining size, the token's live stats;
  verdicts   deterministic (the exit plan: take-profit / trailing / mirrored
             copy sell -> SELL), risk (the stop-loss -> SELL), final (what
             the system did: anything sold -> SELL, else HOLD), and ML (the
             shadow exit model's call, filled later by the ml service).

The ml service labels a checkpoint once FORWARD has passed, from the
token's stored trades: the price change over the next 15 minutes and
whether it fell or rose 10 % in that time. A SELL is right when the price
then fell, a HOLD when it held up. Review data only: nothing here exits a
position.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

FEATURE_VERSION = "exitfeat-2026.10.1"
LABEL_VERSION = "exitlabel-2026.10.1"
SAMPLE_EVERY = timedelta(minutes=5)
FORWARD = timedelta(minutes=15)
MOVE = 0.10  # the "fell / rose 10 %" labels
SELL, HOLD = "SELL", "HOLD"
STRATEGY_EXITS = ("take_profit", "trailing_stop", "copy_sell")
RISK_EXITS = ("stop_loss",)
CHAINS = ("bsc", "robinhood")
LAUNCHPADS = ("fourmeme", "flap", "pons_v2", "pons_v1", "genius_fun")
NUMERIC = ("held_s", "unrealized_pct", "peak_pct", "drawdown_from_peak_pct", "stop_distance_pct", "remaining_fraction",
           "tp_hits", "buys", "sells", "unique_buyers", "net_buy_ratio", "volatility", "liquidity")
FEATURE_NAMES: tuple[str, ...] = NUMERIC + tuple(f"{n}__missing" for n in NUMERIC) + \
    tuple(f"chain_{c}" for c in CHAINS) + tuple(f"lp_{lp}" for lp in LAUNCHPADS)


def _f(v: Any) -> float | None:
    try:
        return float(Decimal(str(v))) if v is not None and v != "" else None
    except Exception:  # noqa: BLE001 - an unreadable value is unknown, never 0
        return None


def verdicts(exit_reasons: list[str]) -> dict[str, str]:
    """deterministic / risk / final from the reasons this tick sold for."""
    strategy = any(r.startswith(STRATEGY_EXITS) for r in exit_reasons)
    risk = any(r.startswith(RISK_EXITS) for r in exit_reasons)
    return {"deterministic": SELL if strategy else HOLD, "risk": SELL if risk else HOLD,
            "final": SELL if exit_reasons else HOLD, "ml": "NOT_AVAILABLE"}


def features(p, price: Decimal, stats: dict[str, Any] | None, state: dict[str, Any] | None, chain: str,
             launchpad: str | None, now: datetime) -> dict[str, Any]:
    entry = _f(p.entry_price)
    px = _f(price)
    high = _f(p.highest_price)
    stop = _f(p.stop_loss)
    init = _f(p.initial_quantity if p.initial_quantity is not None else p.quantity)
    rem = _f(p.remaining_quantity if p.remaining_quantity is not None else p.quantity)
    st = stats or {}
    x: dict[str, Any] = {
        "held_s": (now - p.entry_at).total_seconds() if p.entry_at else None,
        "unrealized_pct": (px / entry - 1) * 100 if px and entry else None,
        "peak_pct": (max(high, px) / entry - 1) * 100 if px and entry and high else None,
        "drawdown_from_peak_pct": (px / max(high, px) - 1) * 100 if px and high else None,
        "stop_distance_pct": (px / stop - 1) * 100 if px and stop else None,
        "remaining_fraction": rem / init if rem is not None and init else None,
        "tp_hits": float(len(p.tp_hits or [])),
        "buys": _f(st.get("buys")), "sells": _f(st.get("sells")), "unique_buyers": _f(st.get("unique_buyers")),
        "net_buy_ratio": _f(st.get("net_buy_ratio")), "volatility": _f(st.get("volatility")),
        "liquidity": _f((state or {}).get("liquidity_quote")),
    }
    for n in NUMERIC:
        x[f"{n}__missing"] = 1.0 if x.get(n) is None else 0.0
    for c in CHAINS:
        x[f"chain_{c}"] = 1.0 if chain == c else 0.0
    for lp in LAUNCHPADS:
        x[f"lp_{lp}"] = 1.0 if launchpad == lp else 0.0
    return x


def labels(p_t: float | None, path: list[tuple[datetime, float]], at: datetime) -> dict[str, Any]:
    """The next FORWARD after a checkpoint, from trade prices (p_t: the last
    trade price at or before the checkpoint)."""
    if not p_t:
        return {"unknown": "no trade price at the checkpoint"}
    pts = [p for t, p in path if at < t <= at + FORWARD and p > 0]
    if not pts:
        return {"forward_return_pct": 0.0, "fell_10": False, "rose_10": False, "trades_after": 0,
                "note": "no trade in the next 15 minutes: price unchanged"}
    return {"forward_return_pct": round((pts[-1] / p_t - 1) * 100, 4), "fell_10": min(pts) <= (1 - MOVE) * p_t,
            "rose_10": max(pts) >= (1 + MOVE) * p_t, "trades_after": len(pts)}


async def record(session, p, price: Decimal, exit_reasons: list[str], row, chain: str, now: datetime) -> bool:
    """Called by data-evm after a management tick (caller commits): a
    checkpoint when the tick sold something or SAMPLE_EVERY has passed
    since the position's last one."""
    from sqlalchemy import func, select

    from yonixalpha_core.db.models import EvmExitSample

    if not exit_reasons:
        last = (await session.execute(select(func.max(EvmExitSample.at)).where(
            EvmExitSample.position_id == p.id))).scalar_one()
        if last is not None and now - last < SAMPLE_EVERY:
            return False
    launchpad = ((p.plan or {}).get("venue") or {}).get("launchpad")
    session.add(EvmExitSample(
        position_id=p.id, chain=chain, token=p.asset_id, engine=p.engine, launchpad=launchpad, at=now,
        price=float(price), features=features(p, price, row.stats if row is not None else None,
                                              row.state if row is not None else None, chain, launchpad, now),
        verdicts=verdicts(exit_reasons), exit_reasons=exit_reasons, feature_version=FEATURE_VERSION,
        label_version=LABEL_VERSION, created_at=now))
    return True


async def label_pending(session, now: datetime, limit: int = 2000) -> int:
    """Labels the checkpoints whose forward window has passed (ml service)."""
    from sqlalchemy import select

    from yonixalpha_core.db.models import EvmExitSample, EvmTrade

    rows = (await session.execute(select(EvmExitSample).where(
        EvmExitSample.labels.is_(None), EvmExitSample.at <= now - FORWARD)
        .order_by(EvmExitSample.at).limit(limit))).scalars().all()
    for r in rows:
        trades = (await session.execute(select(EvmTrade.at, EvmTrade.quote_amount, EvmTrade.token_amount).where(
            EvmTrade.chain == r.chain, EvmTrade.token == r.token, EvmTrade.at <= r.at + FORWARD,
            EvmTrade.at >= r.at - timedelta(hours=1), EvmTrade.token_amount > 0).order_by(EvmTrade.at))).all()
        path = [(t, float(Decimal(q) / Decimal(a))) for t, q, a in trades if a]
        before = [px for t, px in path if t <= r.at]
        r.labels = labels(before[-1] if before else None, path, r.at)
    return len(rows)


def compare(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per recommender, the next 15 minutes after its SELL and its HOLD
    calls: did the price fall after SELL, hold up after HOLD?"""
    def stats(group: list[dict[str, Any]]) -> dict[str, Any]:
        lab = [g["labels"] for g in group if g.get("labels") and "unknown" not in g["labels"]]
        if not lab:
            return {"n": len(group), "labelled": 0}
        rets = [x["forward_return_pct"] for x in lab]
        return {"n": len(group), "labelled": len(lab), "mean_forward_return_pct": round(sum(rets) / len(rets), 4),
                "fell_10_rate": round(sum(x["fell_10"] for x in lab) / len(lab), 4),
                "rose_10_rate": round(sum(x["rose_10"] for x in lab) / len(lab), 4)}

    out: dict[str, Any] = {}
    for who in ("deterministic", "risk", "final", "ml"):
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault((r["verdicts"] or {}).get(who) or "NOT_AVAILABLE", []).append(r)
        out[who] = {k: stats(v) for k, v in sorted(groups.items())}
    out["note"] = ("SELL / HOLD at checkpoints of open paper positions (every 5 min and every exit); outcome = the next "
                   "15 minutes. A recommender adds information when the price falls more after its SELL than after its "
                   "HOLD. ML is a shadow recommendation: it never exits a position.")
    return out
