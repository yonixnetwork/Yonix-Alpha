"""Top-bar summary: paper balances per account (never summed across
currencies), open positions, today's realized PnL, modes, kill switch,
environment locks, and the worst current connection state."""

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import health_state
from app.api.deps import get_current_username, get_db, get_redis, get_settings
from yonixalpha_core import kill_switch
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import Notification, PaperPosition
from yonixalpha_core.safety import store

router = APIRouter(prefix="/summary", tags=["summary"])


@router.get("")
async def summary(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                  settings: Settings = Depends(get_settings), _: str = Depends(get_current_username)) -> dict:
    now = datetime.now(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    killed = await kill_switch.is_engaged(redis)
    accounts = []
    for name in store.DEFAULT_PAPER_ACCOUNTS:
        acct = await store.get_paper_account(db, name)
        state = await store.account_state(db, acct, None, now, killed)
        today, total = (await db.execute(select(
            func.coalesce(func.sum(PaperPosition.realized_pnl).filter(PaperPosition.exit_at >= day), 0),
            func.coalesce(func.sum(PaperPosition.realized_pnl).filter(PaperPosition.exit_at >= acct.reset_at), 0),
        ).where(PaperPosition.account_id == acct.id, PaperPosition.status == "closed"))).one()
        accounts.append({
            "name": name, "currency": acct.quote_currency, "balance": str(acct.cash_balance),
            "available": str(state.available_balance) if state.available_balance is not None else str(acct.cash_balance),
            "equity": str(state.equity) if state.equity is not None else None,
            "open_positions": state.open_positions, "exposure": str(state.current_exposure or Decimal(0)),
            "realized_pnl_today": str(today), "realized_pnl_since_reset": str(total),
        })
    conns = await health_state.connections(db, redis, settings)
    unread = (await db.execute(select(func.count()).select_from(Notification).where(Notification.read_at.is_(None)))).scalar_one()
    await db.commit()
    return {
        "accounts": accounts,
        "open_positions": sum(a["open_positions"] for a in accounts),
        "global_mode": (await store.load_global_mode(db)).value,
        "kill_switch": killed,
        "env": {"trading_enabled": settings.TRADING_ENABLED, "live_trading_enabled": settings.LIVE_TRADING_ENABLED,
                "paper_trading": settings.PAPER_TRADING, "live_permitted": store.live_trading_permitted(settings)},
        "system_status": health_state.worst([c["state"] for c in conns]),
        "connections": {c["name"]: c["state"] for c in conns},
        "unread_notifications": unread,
        "at": now.isoformat(),
    }
