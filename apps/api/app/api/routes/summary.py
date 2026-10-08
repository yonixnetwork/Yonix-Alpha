"""Top-bar summary: paper balances per account (never summed across
currencies), open positions, today's realized PnL, modes, kill switch,
environment locks, and the worst current connection state."""

import json
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import health_state
from app.api.deps import get_current_username, get_db, get_redis, get_settings
from yonixalpha_core import kill_switch, live_trading, position_pnl
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import ExecutionOrder, ModelVersion, Notification, PaperPosition, TradingCandidate
from yonixalpha_core.safety import store
from yonixalpha_core.solana import pump_stream
from yonixalpha_core.state_machine import CandidateState

# Candidates still in play (not rejected, closed or migrated away).
ACTIVE_STATES = [s.value for s in (CandidateState.DISCOVERED, CandidateState.OBSERVING, CandidateState.ANALYZING,
                                   CandidateState.WAITING_FOR_LIQUIDITY, CandidateState.WAITING_FOR_APPROVAL,
                                   CandidateState.QUALIFIED, CandidateState.ENTRY_PENDING)]

router = APIRouter(prefix="/summary", tags=["summary"])


@router.get("")
async def summary(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                  settings: Settings = Depends(get_settings), _: str = Depends(get_current_username)) -> dict:
    now = datetime.now(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    killed = await kill_switch.is_engaged(redis)
    accounts = []
    for name in store.ACTIVE_PAPER_ACCOUNTS:
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


@router.get("/memecoin")
async def memecoin_summary(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                           settings: Settings = Depends(get_settings), _: str = Depends(get_current_username)) -> dict:
    """Dashboard for Solana memecoin trading: live wallet, today's results
    (LIVE and PAPER separately, never summed), market activity, system
    health and open positions. Every number is measured; missing = null."""
    now = datetime.now(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    raw = await redis.get(live_trading.WALLET_KEY)
    w = json.loads(raw) if raw else None
    reserve = (await live_trading.load_live_settings(db)).min_sol_reserve
    wallet = None if w is None else {
        "sol": w.get("sol"), "at": w.get("at"), "token_holdings": w.get("tokens"), "valuation": w.get("valuation"),
        "available_sol": str(max(Decimal(0), Decimal(w["sol"]) - reserve)) if w.get("sol") is not None else None,
        "reserve_sol": str(reserve)}

    today: dict[str, dict] = {}
    for mode in ("LIVE", "PAPER"):
        base = [PaperPosition.execution_mode == mode, PaperPosition.engine.like("solana%")]
        closed = (await db.execute(select(PaperPosition).where(*base, PaperPosition.status == "closed",
                                                               PaperPosition.exit_at >= day))).scalars().all()
        opened = (await db.execute(select(func.count()).select_from(PaperPosition).where(
            *base, PaperPosition.entry_at >= day, PaperPosition.status.in_(("open", "closed", "needs_review"))))).scalar_one()
        open_rows = (await db.execute(select(PaperPosition).where(*base, PaperPosition.status == "open"))).scalars().all()
        unreal = sum(((p.last_price or p.entry_price) - p.entry_price) * (p.remaining_quantity or Decimal(0))
                     for p in open_rows if p.entry_price is not None)
        wins = sum(1 for p in closed if (p.realized_pnl or 0) > 0)
        today[mode] = {"realized_pnl_sol": str(sum((p.realized_pnl or Decimal(0)) for p in closed)),
                       "unrealized_pnl_sol": str(unreal), "trades": opened, "closed": len(closed),
                       "wins": wins, "losses": len(closed) - wins, "open_positions": len(open_rows)}
    lat = (await db.execute(select(ExecutionOrder.diagnostics).where(
        ExecutionOrder.mode == "LIVE", ExecutionOrder.side == "BUY", ExecutionOrder.status == "CONFIRMED",
        ExecutionOrder.created_at >= day))).scalars().all()
    lats = [((d or {}).get("timing") or {}).get("decision_to_confirm_ms") for d in lat]
    lats = [x for x in lats if isinstance(x, (int, float))]
    today["LIVE"]["avg_decision_to_confirm_ms"] = round(sum(lats) / len(lats)) if lats else None

    t = now.timestamp()
    hb = await redis.get(pump_stream.HEARTBEAT)
    stream_age = (now - datetime.fromisoformat(hb)).total_seconds() if hb else None
    market = {
        "fresh_last_hour": await redis.zcount(pump_stream.RECENT, t - 3600, "+inf"),
        "observing": await redis.zcard(pump_stream.OBS_LIVE),
        "migrated_last_hour": await redis.zcount(pump_stream.MIGRATED, t - 3600, "+inf"),
        "momentum_active": (await db.execute(select(func.count()).select_from(TradingCandidate).where(
            TradingCandidate.engine == "momentum", TradingCandidate.state.in_(ACTIVE_STATES)))).scalar_one(),
        "active_opportunities": (await db.execute(select(func.count()).select_from(TradingCandidate).where(
            TradingCandidate.state.in_(ACTIVE_STATES)))).scalar_one(),
    }
    conns = {c["name"]: c for c in await health_state.connections(db, redis, settings)}
    ready = await redis.get(live_trading.READY_KEY)
    rs = json.loads(ready) if ready else None
    ml = (await db.execute(select(func.count()).select_from(ModelVersion).where(ModelVersion.status == "active"))).scalar_one()
    solana = {k: v for k, v in conns.items() if v.get("category") in ("solana", "infrastructure")}
    system = {
        # Solana-side connections only; a module that is not configured
        # (e.g. an unused engine) is not a problem.
        "rpc": {k: v["state"] for k, v in solana.items()},
        "problems": {k: {"state": v["state"], "detail": v.get("detail")} for k, v in solana.items()
                     if v["state"] not in ("CONNECTED", *health_state.NOT_COUNTED)},
        "data": {"stream_heartbeat_age_seconds": round(stream_age, 1) if stream_age is not None else None,
                 "state": "LIVE" if stream_age is not None and stream_age < 60 else "STALE" if stream_age is not None else "UNAVAILABLE"},
        "execution": {"state": (rs or {}).get("status", "UNKNOWN"), "reason": (rs or {}).get("reason")},
        "ml": {"active_models": ml},
        "connections": {k: v["state"] for k, v in conns.items()},
    }
    open_positions = (await db.execute(select(PaperPosition).where(
        PaperPosition.engine.like("solana%"), PaperPosition.status.in_(("open", "pending_entry", "needs_review")))
        .order_by(PaperPosition.entry_at.desc()).limit(50))).scalars().all()
    positions = [{"id": str(p.id), "symbol": p.symbol, "mint": p.asset_id, "mode": p.execution_mode, "status": p.status,
                  "route": p.execution_route, "entry_price": str(p.entry_price), "last_price": str(p.last_price) if p.last_price is not None else None,
                  "pnl_sol": str(((p.last_price or p.entry_price) - p.entry_price) * (p.remaining_quantity or Decimal(0))),
                  "pnl_pct": str(((p.last_price / p.entry_price - 1) * 100).quantize(Decimal("0.01"))) if p.last_price and p.entry_price else None,
                  "age_seconds": round((now - p.entry_at).total_seconds()) if p.entry_at else None,
                  "last_marked_at": p.last_marked_at.isoformat() if p.last_marked_at else None,
                  "pnl": position_pnl.view(p, now, 60 if p.execution_mode == "LIVE" else 120)} for p in open_positions]
    await db.commit()
    return {"wallet": wallet, "today": today, "market": market, "system": system, "positions": positions,
            "global_mode": (await store.load_global_mode(db)).value, "kill_switch": await kill_switch.is_engaged(redis),
            "at": now.isoformat()}
