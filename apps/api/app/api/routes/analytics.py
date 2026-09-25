"""Performance analytics over closed paper trades, and Gold vs BTC ratio
analytics.

Trades are grouped by paper account first, because accounts are in
different currencies (SOL, USDT, USDC) and must never be summed together.
Open positions are not trades yet: they are counted separately and never
enter a win rate. By default only trades since each account's last reset
count, so a reset really does start a fresh record.
"""

import json
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis
from yonixalpha_core.analytics import ClosedTrade, performance
from yonixalpha_core.db.models import PaperPosition, RiskAssessment, StrategyState
from yonixalpha_core.safety import store
from yonixalpha_core.strategies import gold_btc, grid
from yonixalpha_core.venues.common import VenueError

router = APIRouter(prefix="/analytics", tags=["analytics"])

GOLD_BTC_CACHE_SECONDS = 60


def venue_of(p: PaperPosition) -> str:
    v = ((p.plan or {}).get("venue") or {})
    if v.get("venue"):
        return v["venue"]
    return "solana" if (p.engine or "").startswith("solana") else (p.engine or "unknown")


def _trade(p: PaperPosition) -> ClosedTrade:
    return ClosedTrade(pnl=p.realized_pnl or Decimal(0), pnl_pct=p.realized_pnl_pct, fees=p.fees_paid_quote or Decimal(0),
                       entry_at=p.entry_at, exit_at=p.exit_at or p.entry_at)


def _without_curve(stats: dict) -> dict:
    return {k: v for k, v in stats.items() if k != "equity_curve"}


@router.get("/performance")
async def performance_report(
    account: str | None = None,
    engine: str | None = None,
    strategy: str | None = None,
    venue: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    include_before_reset: bool = False,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(get_current_username),
) -> dict:
    out = []
    names = [account] if account else list(store.DEFAULT_PAPER_ACCOUNTS)
    for name in names:
        if name not in store.DEFAULT_PAPER_ACCOUNTS:
            raise HTTPException(404, f"unknown account; one of {list(store.DEFAULT_PAPER_ACCOUNTS)}")
        acct = await store.get_paper_account(db, name)
        q = (select(PaperPosition, RiskAssessment.strategy)
             .outerjoin(RiskAssessment, RiskAssessment.id == PaperPosition.assessment_id)
             .where(PaperPosition.account_id == acct.id, PaperPosition.status == "closed"))
        if not include_before_reset:
            q = q.where(PaperPosition.exit_at >= acct.reset_at)
        if engine:
            q = q.where(PaperPosition.engine == engine)
        if strategy:
            q = q.where(func.coalesce(RiskAssessment.strategy, PaperPosition.engine) == strategy)
        if since:
            q = q.where(PaperPosition.exit_at >= since)
        if until:
            q = q.where(PaperPosition.exit_at <= until)
        rows = [(p, s or p.engine or "unknown") for p, s in (await db.execute(q)).all()]
        if venue:
            rows = [(p, s) for p, s in rows if venue_of(p) == venue]
        open_count = (await db.execute(select(func.count()).select_from(PaperPosition).where(
            PaperPosition.account_id == acct.id, PaperPosition.status == "open"))).scalar_one()

        def group(key) -> dict:
            buckets: dict[str, list] = {}
            for p, s in rows:
                buckets.setdefault(key(p, s), []).append(_trade(p))
            return {k: _without_curve(performance(v)) for k, v in sorted(buckets.items())}

        out.append({
            "account": name,
            "currency": acct.quote_currency,
            "starting_balance": str(acct.starting_balance),
            "reset_at": acct.reset_at.isoformat(),
            "open_positions_not_counted": open_count,
            "overall": performance([_trade(p) for p, _ in rows], acct.starting_balance),
            "by_strategy": group(lambda p, s: s),
            "by_engine": group(lambda p, s: p.engine or "unknown"),
            "by_venue": group(lambda p, s: venue_of(p)),
        })
    grids = (await db.execute(select(StrategyState).where(StrategyState.strategy == "hyperliquid_grid"))).scalars().all()
    grid_rows = [{"coin": g.key, **grid.session_summary(g.status, g.state or {})} for g in grids]
    return {
        "accounts": out,
        "grid": grid_rows,
        "notes": ["Currencies are never mixed: each account reports in its own quote currency.",
                  "Open positions are excluded from every statistic until they close.",
                  "Grid results are reported as session equity vs reserved capital, not per-trade win/loss.",
                  "Paper results simulate fills from live books/curves; they are not evidence of live profitability."],
    }


@router.get("/gold-btc")
async def gold_vs_btc(request: Request, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                      _: str = Depends(get_current_username)) -> dict:
    params = {**gold_btc.DEFAULTS, **await store.load_strategy_config(db, "gold_vs_btc")}
    key = "yx:cache:gold_btc:" + json.dumps(params, sort_keys=True, default=str)
    cached = await redis.get(key)
    if cached:
        return json.loads(cached)
    venue = request.app.state.venues["binance"]
    try:
        btc = await venue.klines(params["btc"], params["interval"], int(params["candles"]))
        gold = await venue.klines(params["gold"], params["interval"], int(params["candles"]))
        a = gold_btc.analyse(btc, gold, params)
    except VenueError as exc:
        raise HTTPException(502, f"Binance market data unavailable: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    body = {**{k: v for k, v in asdict(a).items() if k != "points"}, "points": [asdict(p) for p in a.points],
            "params": params, "source": f"Binance USDⓈ-M {params['btc']} / {params['gold']} closed {params['interval']} candles",
            "note": "Descriptive analytics only — no trading signal is generated."}
    await redis.set(key, json.dumps(body, default=str), ex=GOLD_BTC_CACHE_SECONDS)
    return body
