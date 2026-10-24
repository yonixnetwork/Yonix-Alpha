"""Performance analytics over closed paper trades.

Trades are grouped by paper account first, because accounts are in
different currencies (SOL, USDT, USDC) and must never be summed together.
Open positions are not trades yet: they are counted separately and never
enter a win rate. By default only trades since each account's last reset
count, so a reset really does start a fresh record.
"""

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.api import review_cache
from app.api.routes.ml import _review
from app.api.util import audit, jsonable, user_id
from yonixalpha_core import forward_test, solana_performance, system_profile
from yonixalpha_core.analytics import ClosedTrade, performance
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import PaperPosition, PlatformSetting, RiskAssessment
from yonixalpha_core.safety import store

router = APIRouter(prefix="/analytics", tags=["analytics"])


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
    settings: Settings = Depends(get_settings),
    _: str = Depends(get_current_username),
) -> dict:
    out = []
    names = [account] if account else [n for n in store.ACTIVE_PAPER_ACCOUNTS if system_profile.account_enabled(settings, n)]
    for name in names:
        if name not in store.ACTIVE_PAPER_ACCOUNTS:
            raise HTTPException(404, f"unknown account; one of {list(store.ACTIVE_PAPER_ACCOUNTS)}")
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
    return {
        "accounts": out,
        "notes": ["Currencies are never mixed: each account reports in its own quote currency.",
                  "Open positions are excluded from every statistic until they close.",
                  "Paper results simulate fills from live books/curves; they are not evidence of live profitability."],
    }


@router.get("/solana-performance")
async def solana_performance_report(request: Request, days: int = Query(7, ge=1, le=solana_performance.MAX_DAYS),
                                    outcomes: bool = True, refresh: bool = Query(False),
                                    engine: str | None = Query(None, pattern="^solana_(fresh|migration|momentum)$"),
                                    redis: Redis = Depends(get_redis), _: str = Depends(get_current_username)) -> dict:
    """Solana PAPER vs LIVE: decisions, entries, closed trades by stage /
    hold time / entry quality / exit reason, missed winners, false positives
    and LIVE execution telemetry (yonixalpha_core.solana_performance).
    Computed in the background like ML Review (app.api.review_cache): a week
    of decisions took longer than the 25 s request limit on the server
    (2026-10-09); 202 until the first result is ready."""
    async def compute(db: AsyncSession) -> dict:
        return await solana_performance.report(db, days, outcomes=outcomes, engine=engine)

    return await _review(request, redis, f"solana-performance:{days}:{int(outcomes)}:{engine or 'all'}", compute, refresh)


@router.get("/solana-positions")
async def solana_positions(engine: str | None = Query(None, pattern="^solana_(fresh|migration|momentum)$"),
                           mode: str | None = Query(None, pattern="^(PAPER|LIVE)$"), limit: int = Query(50, ge=1, le=500),
                           db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    """Latest Solana positions one by one (solana_performance.positions):
    booked PnL for closed positions only; an open position's marked value is
    labelled unrealized."""
    return {"positions": await solana_performance.positions(db, engine=engine, mode=mode, limit=limit),
            "note": "net = booked realized PnL after every recorded cost; gross = net + fees; the slippage estimate "
                    "is inside gross and net already. Open positions are never booked."}


@router.get("/forward-test")
async def forward_test_report(request: Request, refresh: bool = Query(False), redis: Redis = Depends(get_redis),
                              _: str = Depends(get_current_username)) -> dict:
    """The current forward-test period against its baseline
    (yonixalpha_core.forward_test); computed in the background cache."""
    async def compute(db: AsyncSession) -> dict:
        return await forward_test.report(db)

    return await _review(request, redis, "forward-test", compute, refresh)


@router.get("/forward-test/history")
async def forward_test_history(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    return {"periods": await forward_test.history(db),
            "deployment": ((await db.get(PlatformSetting, forward_test.DEPLOY_KEY)) or PlatformSetting(value={})).value}


@router.post("/forward-test/start")
async def forward_test_start(body: dict, request: Request, db: AsyncSession = Depends(get_db),
                             redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)) -> dict:
    """Opens a new forward-test period now (audited); the previous one is
    ended and kept. Changes no trading setting."""
    label = str(body.get("label") or "").strip()
    if len(label) < 3:
        raise HTTPException(422, "give the period a label (for example the deployed revision)")
    period = await forward_test.start(db, redis, label=label, user=username, user_id=await user_id(db, username))
    await audit(db, username, request, "forward_test.start_requested", {"id": period["id"], "label": period["label"]})
    await db.commit()
    # the next report is computed for the new period, never served from the old one
    await redis.delete(review_cache.PREFIX + "forward-test", review_cache.PREFIX + "forward-test:last")
    return jsonable({k: period.get(k) for k in ("id", "label", "started_at", "started_by", "deployment", "data_sources",
                                                 "rules", "configuration")})
