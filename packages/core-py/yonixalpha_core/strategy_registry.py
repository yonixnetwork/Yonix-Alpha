"""Entry strategy registry (regression recovery, 2026-10-10).

One record per strategy: identity, version, token category, thesis, the
signal it uses, its inputs and whether it may open a (PAPER) trade by
itself. The decision functions are in entry_intel (fresh / momentum, from
the pump stream) and entry_outcomes.migrated_triggers (migrated, from
PumpSwap pool reserves). Every signal is recorded in entry_signals under
`recorded_as` and labelled by the same labeller, so every strategy is
compared with the existing pipeline (CURRENT_GATE_ENTRY) on identical terms.

Routing (entry_intel.route): per token, only the strategies of its
category are considered, the highest-scoring CANDIDATE is selected, every
other strategy's reason is kept, and no qualifying strategy is NO_TRADE.
A token gets at most one routed paper candidate (claim `yx:ee:route:`),
so a loss on a token never leads to another strategy re-entering it.

Mode changes:
  promotion  manual only (operator, audited); a strategy never promotes
             itself, and none of them can create a LIVE order
  demotion   automatic, PAPER -> SHADOW, only on statistical evidence:
             at least DEMOTION_MIN_SIGNALS labelled executable returns
             among its newest DEMOTION_WINDOW signals AND the upper bound
             of a 95% confidence interval of their mean below zero. One or
             two losses can never trigger it. Each demotion is audited.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from yonixalpha_core import entry_intel as ei

DEMOTION_MIN_SIGNALS = 30
DEMOTION_WINDOW = 60
DEMOTION_Z = 1.96  # two-sided 95%: demote only when even the optimistic bound of the mean is a loss
ROUTE_CLAIM = "yx:ee:route:"
ROUTE_CLAIM_TTL = 3 * 86400
WHY_KEY = "yx:ee:why:"  # hash per (strategy, UTC day): normalized blocking reason -> evaluations
ROUTE_STATS = "yx:ee:routes:"  # hash per UTC day: selected:<strategy> / no_trade:<category> -> count
STATS_TTL = 3 * 86400
_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class StrategySpec:
    code: str
    recorded_as: str
    version: str
    category: str
    title: str
    thesis: str
    signal: str
    inputs: tuple[str, ...]
    trades_alone: bool = True
    paper_allowed: bool = True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["inputs"] = list(self.inputs)
        return d


SPECS: tuple[StrategySpec, ...] = (
    StrategySpec("F1", ei.EARLY_ACCELERATION, "1", ei.FRESH, "Early acceleration",
                 "Demand is accelerating in the first minutes, before the move is obvious.",
                 "net SOL inflow per 10 s rising, with new buyers and no single-wallet push",
                 ("w10 / w10_prev net SOL", "new buyers per 10 s", "top buyer share", "displacement"),),
    StrategySpec("F2", ei.EARLY_DEMAND_CONFIRMATION, "1", ei.FRESH, "Early demand confirmation",
                 "The buyer base is broadening (many independent buyers, little concentration), not one burst.",
                 "buyer breadth and 30 s net buy pressure; deliberately ignores price and inflow acceleration",
                 ("unique buyers", "meaningful independent buyers", "net buy pressure 30 s", "top buyer share 30 s",
                  "new buyers vs new sellers"),),
    StrategySpec("F3", ei.SELECTIVE_EARLY_BREAKOUT, "1", ei.FRESH, "Selective early breakout",
                 "Price leaves a range it has already tested, with fresh buying in the breakout.",
                 "price above the high before the last 10 s after a real pullback, backed by new buyers",
                 ("max pullback so far", "price vs prior high", "w10 net SOL", "new buyers", "displacement"),),
    StrategySpec("M1", ei.MIGRATED_DELAYED_CONFIRMATION, "1", ei.MIGRATION, "Post-migration stabilization",
                 "After migration, wait until the pool has held its migration price with liquidity above the start.",
                 ">= 120 s after migration, price >= migration price, pool SOL above the initial",
                 ("pool reserves samples",), paper_allowed=False),
    StrategySpec("M2", ei.MIGRATED_CONTINUATION, "1", ei.MIGRATION, "Migration continuation",
                 "The migrated pool makes a new high while liquidity keeps rising.",
                 "new high in the pool price with rising pool SOL",
                 ("pool reserves samples",), paper_allowed=False),
    StrategySpec("M3", ei.MIGRATED_PULLBACK, "1", ei.MIGRATION, "Post-migration pullback and recovery",
                 "A pullback after migration that turns back up while liquidity rises.",
                 ">= 10% pullback, +3% off the low, rising pool SOL",
                 ("pool reserves samples",), paper_allowed=False),
    StrategySpec("P1", ei.MOMENTUM_CONTINUATION, "1", ei.MOMENTUM, "Momentum continuation",
                 "An established move with a healthy structure continues after a pullback.",
                 "phase HEALTHY_CONTINUATION, 60 s net inflow, inflow not halving, buyers outgrowing sellers",
                 ("momentum phase", "w60 net SOL", "drawdown from peak", "new buyers vs sellers"),),
    StrategySpec("P2", ei.BREAKOUT_RETEST, "1", ei.MOMENTUM, "Breakout and retest",
                 "A shallow pullback to support holds and buyers return; not a chase.",
                 "5-20% below the high, a bounce off the retest low, net inflow rising again",
                 ("drawdown from peak", "seconds since peak", "bounce from low", "w10 net SOL", "new buyers"),),
    StrategySpec("P3", ei.MOMENTUM_RECOVERY, "1", ei.MOMENTUM, "Momentum reversal / recovery",
                 "After a real drop, selling objectively declines and demand returns; never a falling-knife buy.",
                 "25-70% below the high, sell SOL per 10 s falling below buys, new buyers, a confirmed turn",
                 ("drawdown from peak", "sell SOL per 10 s", "new buyers", "bounce from low"),),
    StrategySpec("SW", ei.SMART_WALLET_CONFIRMATION, "1", "ANY", "Smart-wallet confirmation",
                 "Proven wallets entered the routed candidate recently and have not exited.",
                 "corroborating evidence only, evaluated on the routed candidate",
                 ("launch_buyers outcomes resolved before the decision",), trades_alone=False, paper_allowed=False),
)
BY_NAME = {s.recorded_as: s for s in SPECS}
BY_CODE = {s.code: s for s in SPECS}


def version_of(name: str) -> str | None:
    s = BY_NAME.get(name)
    return f"{s.code}.v{s.version}/{ei.FEATURE_VERSION}" if s else None


def specs() -> list[dict[str, Any]]:
    return [s.to_dict() for s in SPECS]


async def claim_route(redis, mint: str, strategy: str) -> bool:
    """At most one routed paper candidate per token, whatever its outcome."""
    if redis is None:
        return True
    return bool(await redis.set(f"{ROUTE_CLAIM}{mint}", strategy, nx=True, ex=ROUTE_CLAIM_TTL))


def reason_key(reason: str) -> str:
    """'662s old: past the window (300s)' -> 'Ns old: past the window (Ns)':
    a bounded set of counter fields, whatever the numbers."""
    return _NUM.sub("N", reason)[:120]


def _s(x) -> str:
    return x.decode() if isinstance(x, bytes) else x


async def top_reasons(redis, name: str, now: datetime, days: int = 2, top: int = 5) -> list[tuple[str, int]]:
    """Most frequent first blocking reason of a strategy (non-CANDIDATE
    evaluations), over the last `days` UTC days."""
    total: dict[str, int] = {}
    for i in range(days):
        day = (now - timedelta(days=i)).strftime("%Y%m%d")
        for k, v in (await redis.hgetall(f"{WHY_KEY}{name}:{day}")).items():
            total[_s(k)] = total.get(_s(k), 0) + int(v)
    return sorted(total.items(), key=lambda kv: -kv[1])[:top]


async def route_stats(redis, now: datetime, days: int = 2) -> dict[str, int]:
    total: dict[str, int] = {}
    for i in range(days):
        day = (now - timedelta(days=i)).strftime("%Y%m%d")
        for k, v in (await redis.hgetall(f"{ROUTE_STATS}{day}")).items():
            total[_s(k)] = total.get(_s(k), 0) + int(v)
    return total


def deterioration(returns: list[float]) -> dict[str, Any]:
    """Is the newest-signal mean executable return reliably negative?
    `returns` newest last; only the newest DEMOTION_WINDOW are used."""
    r = returns[-DEMOTION_WINDOW:]
    out: dict[str, Any] = {"n": len(r), "min_n": DEMOTION_MIN_SIGNALS, "rule": (
        f"demote PAPER -> SHADOW when >= {DEMOTION_MIN_SIGNALS} of the newest {DEMOTION_WINDOW} labelled signals have "
        f"an executable return and the upper bound of the 95% interval of their mean is below 0")}
    if len(r) < DEMOTION_MIN_SIGNALS:
        out.update({"deteriorated": False, "reason": f"{len(r)} labelled returns (need {DEMOTION_MIN_SIGNALS})"})
        return out
    mean = statistics.fmean(r)
    sd = statistics.stdev(r)
    upper = mean + DEMOTION_Z * sd / math.sqrt(len(r))
    out.update({"mean_pct": round(mean, 3), "sd_pct": round(sd, 3), "upper_95_pct": round(upper, 3),
                "deteriorated": upper < 0})
    out["reason"] = (f"mean {mean:+.2f}%, 95% upper bound {upper:+.2f}% < 0: reliably losing" if upper < 0
                     else f"mean {mean:+.2f}%, 95% upper bound {upper:+.2f}%: not reliably losing")
    return out


def demotions(returns_by_strategy: dict[str, list[float]], modes: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Strategies in PAPER whose recent signals are reliably losing."""
    out = {}
    for name, mode in modes.items():
        if mode != "PAPER":
            continue
        d = deterioration(returns_by_strategy.get(name, []))
        if d["deteriorated"]:
            out[name] = d
    return out


async def apply_demotions(session, now: datetime) -> dict[str, Any]:
    """Loads the newest labelled returns of the PAPER-mode strategies only
    (bounded: DEMOTION_WINDOW rows each), demotes the reliably losing ones
    to SHADOW and audits each change. Never promotes."""
    from sqlalchemy import select

    from yonixalpha_core import entry_store
    from yonixalpha_core.db.models import AuditLog, EntrySignal, PlatformSetting

    s = await entry_store.load_settings(session)
    paper = [n for n, m in s["modes"].items() if m == "PAPER"]
    if not paper:
        return {"checked": 0}
    returns: dict[str, list[float]] = {}
    for name in paper:
        rows = (await session.execute(
            select(EntrySignal.outcome).where(EntrySignal.strategy == name, EntrySignal.outcome_at.is_not(None))
            .order_by(EntrySignal.decided_at.desc()).limit(DEMOTION_WINDOW))).scalars().all()
        vals = [float(v) for o in reversed(rows) if (v := (o or {}).get("executable_return_pct")) is not None]
        returns[name] = vals
    dem = demotions(returns, s["modes"])
    if not dem:
        return {"checked": len(paper), "demoted": []}
    row = await session.get(PlatformSetting, entry_store.SETTINGS_KEY)
    before = dict(row.value) if row else {}
    value = {**before, "modes": {**(before.get("modes") or {}), **{n: "SHADOW" for n in dem}}}
    if row is None:
        session.add(PlatformSetting(key=entry_store.SETTINGS_KEY, value=value))
    else:
        row.value = value
    for name, d in dem.items():
        session.add(AuditLog(event_type="entry_intel.strategy_demoted",
                             detail={"strategy": name, "from": "PAPER", "to": "SHADOW", "at": now.isoformat(),
                                     "evidence": d, "by": "automatic deterioration rule (promotion stays manual)"}))
    await session.commit()
    return {"checked": len(paper), "demoted": sorted(dem)}
