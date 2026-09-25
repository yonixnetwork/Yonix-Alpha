"""Exchange venues: market data and account status for Binance USDⓈ-M,
Bybit and Hyperliquid.

Account calls are read-only (balances, positions, orders); nothing here can
place, amend or cancel an order. A venue's account is reported as
- NOT CONNECTED: credentials (or, for Hyperliquid, the address) not set;
- IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION: set, but no successful
  authenticated call has been observed yet;
- VERIFIED (READ-ONLY): an authenticated read succeeded, with its time.
Credentials are never returned or logged — only whether they are set.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import health_state
from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.api.util import jsonable
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import Fill, Order, PnlRecord, Position
from yonixalpha_core.safety import store
from yonixalpha_core.venues.common import NotConfigured, VenueError
from yonixalpha_core.venues.registry import VENUE_ENGINE

router = APIRouter(prefix="/venues", tags=["venues"])

TERMINAL_ORDER_STATUSES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "not_found", "submit_failed"}
VERIFIED_KEY = "yx:venue:verified:{}"
NOT_CONNECTED = "NOT CONNECTED"
AWAITING = "IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION"
VERIFIED = "VERIFIED (READ-ONLY)"
ACCOUNTS = {"binance": "binance_futures", "bybit": "bybit_futures", "hyperliquid": "hyperliquid"}


def _configured(venue: str, settings: Settings) -> bool:
    if venue == "binance":
        return bool(settings.BINANCE_API_KEY and settings.BINANCE_API_SECRET)
    if venue == "bybit":
        return bool(settings.BYBIT_API_KEY and settings.BYBIT_API_SECRET)
    return bool(settings.HYPERLIQUID_ACCOUNT_ADDRESS)


async def _account_status(venue: str, settings: Settings, redis: Redis, db: AsyncSession) -> dict[str, Any]:
    if not _configured(venue, settings):
        return {"status": NOT_CONNECTED, "verified_at": None}
    verified = await redis.get(VERIFIED_KEY.format(venue))
    if venue == "binance" and not verified:
        # engine-binance-futures syncs positions with the signed API; a recent
        # sync row is the evidence that the credentials work.
        last = (await db.execute(select(func.max(Position.updated_at)))).scalar_one()
        if last and datetime.now(timezone.utc) - last < timedelta(minutes=15):
            verified = last.isoformat()
    return {"status": VERIFIED if verified else AWAITING, "verified_at": verified}


def _venue(request: Request, venue: str):
    v = request.app.state.venues.get(venue)
    if v is None:
        raise HTTPException(404, "unknown venue; one of binance, bybit, hyperliquid")
    return v


@router.get("")
async def list_venues(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                      settings: Settings = Depends(get_settings), _: str = Depends(get_current_username)) -> list[dict]:
    conns = {c["name"]: c for c in await health_state.connections(db, redis, settings)}
    out = []
    for venue, engine in VENUE_ENGINE.items():
        acct = await store.get_paper_account(db, ACCOUNTS[venue])
        out.append({
            "venue": venue, "engine": engine, "mode": (await store.load_strategy_mode(db, engine)).value,
            "credentials_configured": _configured(venue, settings),
            "account": await _account_status(venue, settings, redis, db),
            "market_data": conns.get(venue),
            "paper_account": {"name": acct.name, "currency": acct.quote_currency, "cash": str(acct.cash_balance)},
            "live_orders": "DISABLED — live execution is not implemented; environment locks keep trading off",
        })
    await db.commit()
    return out


@router.get("/{venue}/market")
async def market(venue: str, request: Request, symbol: str = Query(..., pattern=r"^[A-Z0-9]{1,20}$"),
                 _: str = Depends(get_current_username)) -> dict:
    v = _venue(request, venue)
    try:
        ticker = await v.ticker(symbol)
        book = await v.book(symbol)
    except VenueError as exc:
        raise HTTPException(502, f"{venue} market data unavailable: {exc}") from exc
    return jsonable({"venue": venue, "symbol": symbol, "ticker": ticker.__dict__,
                     "book": {"mid": book.mid, "spread_bps": book.spread_bps, "liquidity_quote_within_band": book.liquidity_quote,
                              "best_bid": book.bids[0] if book.bids else None, "best_ask": book.asks[0] if book.asks else None}})


@router.get("/binance/account")
async def binance_account(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                          settings: Settings = Depends(get_settings), _: str = Depends(get_current_username)) -> dict:
    """From the tables engine-binance-futures maintains (it owns the signed
    client and the user-data stream); the API never calls signed Binance
    endpoints itself."""
    positions = (await db.execute(select(Position).where(Position.position_amt != 0))).scalars().all()
    orders = (await db.execute(select(Order).where(Order.status.notin_(TERMINAL_ORDER_STATUSES))
                               .order_by(Order.created_at.desc()).limit(100))).scalars().all()
    fills = (await db.execute(select(Fill).order_by(Fill.occurred_at.desc()).limit(50))).scalars().all()
    since = datetime.now(timezone.utc) - timedelta(days=7)
    income = (await db.execute(select(PnlRecord.income_type, PnlRecord.asset, func.sum(PnlRecord.income))
                               .where(PnlRecord.occurred_at >= since).group_by(PnlRecord.income_type, PnlRecord.asset))).all()
    return jsonable({
        "account": await _account_status("binance", settings, redis, db),
        "positions": [{"symbol": p.symbol, "amount": p.position_amt, "entry": p.entry_price, "mark": p.mark_price,
                       "unrealized_pnl": p.unrealized_pnl, "leverage": p.leverage, "liquidation": p.liquidation_price,
                       "updated_at": p.updated_at} for p in positions],
        "open_orders": [{"symbol": o.symbol, "side": o.side, "type": o.order_type, "qty": o.quantity, "price": o.price,
                         "status": o.status, "created_at": o.created_at} for o in orders],
        "recent_fills": [{"symbol": f.symbol, "side": f.side, "price": f.price, "qty": f.quantity, "commission": f.commission,
                          "realized_pnl": f.realized_pnl, "at": f.occurred_at} for f in fills],
        "income_7d": [{"type": t, "asset": a, "total": v} for t, a, v in income],
    })


async def _live_account(venue: str, request: Request, redis: Redis, settings: Settings) -> dict:
    if not _configured(venue, settings):
        raise HTTPException(409, NOT_CONNECTED + (": set BYBIT_API_KEY / BYBIT_API_SECRET in .env" if venue == "bybit"
                                                  else ": set HYPERLIQUID_ACCOUNT_ADDRESS in .env"))
    v = _venue(request, venue)
    try:
        if venue == "bybit":
            body = {"wallet": await v.wallet_balance(), "positions": await v.positions(), "open_orders": await v.open_orders()}
        else:
            body = {"account": await v.account(), "open_orders": await v.open_orders()}
    except NotConfigured as exc:
        raise HTTPException(409, str(exc)) from exc
    except VenueError as exc:
        raise HTTPException(502, f"{venue} account read failed: {exc}") from exc
    now = datetime.now(timezone.utc).isoformat()
    await redis.set(VERIFIED_KEY.format(venue), now)
    return jsonable({"account": {"status": VERIFIED, "verified_at": now}, **body})


@router.get("/bybit/account")
async def bybit_account(request: Request, redis: Redis = Depends(get_redis), settings: Settings = Depends(get_settings),
                        _: str = Depends(get_current_username)) -> dict:
    return await _live_account("bybit", request, redis, settings)


@router.get("/hyperliquid/account")
async def hyperliquid_account(request: Request, redis: Redis = Depends(get_redis), settings: Settings = Depends(get_settings),
                              _: str = Depends(get_current_username)) -> dict:
    return await _live_account("hyperliquid", request, redis, settings)
