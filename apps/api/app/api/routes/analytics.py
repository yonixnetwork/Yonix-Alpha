"""Performance analytics over closed paper trades.

Trades are grouped by paper account first, because accounts are in
different currencies (SOL, USDT, USDC) and must never be summed together.
Open positions are not trades yet: they are counted separately and never
enter a win rate. By default only trades since each account's last reset
count, so a reset really does start a fresh record.
"""

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db
from yonixalpha_core import solana_performance
from yonixalpha_core.analytics import ClosedTrade, performance
from yonixalpha_core.db.models import PaperPosition, RiskAssessment
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
    _: str = Depends(get_current_username),
) -> dict:
    out = []
    names = [account] if account else list(store.ACTIVE_PAPER_ACCOUNTS)
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
async def solana_performance_report(days: int = Query(7, ge=1, le=solana_performance.MAX_DAYS), outcomes: bool = True,
                                    db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    """Solana PAPER vs LIVE: decisions, entries, closed trades by stage /
    hold time / entry quality / exit reason, missed winners, false positives
    and LIVE execution telemetry (yonixalpha_core.solana_performance)."""
    return await solana_performance.report(db, days, outcomes=outcomes)
