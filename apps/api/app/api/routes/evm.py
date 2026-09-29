"""EVM chains (BSC, Robinhood Chain): discovered tokens with category, stats,
safety and the last entry decision; per-token trades; paper positions; and
the EVM trading settings (amounts in BNB / ETH)."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from redis.asyncio import Redis
from sqlalchemy import desc, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.api.util import audit, jsonable
from yonixalpha_core import events
from yonixalpha_core.chains.evm import settings as evm_settings
from yonixalpha_core.chains.evm import wallet as evm_wallet
from yonixalpha_core.chains.evm.rpc import make_rpc
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import EvmToken, EvmTrade, PaperAccount, PaperPosition, PlatformSetting

router = APIRouter(prefix="/evm", tags=["evm"])
CHAIN = "^(bsc|robinhood)$"


def _token(r: EvmToken, full: bool = False) -> dict:
    d = {"chain": r.chain, "token": r.token, "launchpad": r.launchpad, "name": r.name, "symbol": r.symbol,
         "creator": r.creator, "created_at": r.created_at, "category": r.category, "stage": r.stage,
         "migrated_at": r.migrated_at, "safety_verdict": r.safety_verdict, "safety_at": r.safety_at,
         "stats": r.stats, "last_trade_at": r.last_trade_at, "launch_seen": bool((r.extra or {}).get("launch_seen")),
         "entry_decision": (r.extra or {}).get("entry_decision")}
    if full:
        d.update(venue=r.venue, quote_token=r.quote_token, migration=r.migration, state=r.state, state_at=r.state_at,
                 safety=r.safety, created_block=r.created_block, created_tx=r.created_tx, extra=r.extra)
    return d


@router.get("/tokens")
async def tokens(chain: str | None = Query(None, pattern=CHAIN), category: str | None = Query(None),
                 launchpad: str | None = None, active_minutes: int = Query(60, ge=1, le=10080),
                 limit: int = Query(100, ge=1, le=500), db: AsyncSession = Depends(get_db),
                 _: str = Depends(get_current_username)) -> dict:
    since = datetime.now(timezone.utc) - timedelta(minutes=active_minutes)
    q = select(EvmToken).where(func.coalesce(EvmToken.last_trade_at, EvmToken.created_at) >= since)
    if chain:
        q = q.where(EvmToken.chain == chain)
    if category:
        q = q.where(EvmToken.category == category.upper())
    if launchpad:
        q = q.where(EvmToken.launchpad == launchpad)
    rows = (await db.execute(q.order_by(desc(func.coalesce(EvmToken.last_trade_at, EvmToken.created_at))).limit(limit))).scalars().all()
    counts = dict((await db.execute(select(EvmToken.category, func.count()).where(
        func.coalesce(EvmToken.last_trade_at, EvmToken.created_at) >= since,
        *([EvmToken.chain == chain] if chain else [])).group_by(EvmToken.category))).all())
    return jsonable({"tokens": [_token(r) for r in rows], "categories": counts,
                     "note": "EVM discovery, quotes and safety run on the real chains; paper only (no EVM live execution)"})


@router.get("/tokens/{chain}/{token}")
async def token_detail(chain: str, token: str, db: AsyncSession = Depends(get_db),
                       _: str = Depends(get_current_username)) -> dict:
    row = (await db.execute(select(EvmToken).where(EvmToken.chain == chain,
                                                   func.lower(EvmToken.token) == token.lower()))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "token not discovered")
    trades = (await db.execute(select(EvmTrade).where(EvmTrade.chain == chain, EvmTrade.token == row.token)
                               .order_by(desc(EvmTrade.at)).limit(200))).scalars().all()
    positions = (await db.execute(select(PaperPosition).where(
        PaperPosition.engine == f"evm_{chain}", func.lower(PaperPosition.asset_id) == row.token.lower())
        .order_by(desc(PaperPosition.entry_at)))).scalars().all()
    return jsonable({"token": _token(row, full=True),
                     "trades": [{"event_id": t.event_id, "trader": t.trader, "side": "BUY" if t.is_buy else "SELL",
                                 "token_amount": str(t.token_amount), "quote_amount": str(t.quote_amount / 10 ** 18),
                                 "at": t.at, "block": t.block, "tx_hash": t.tx_hash} for t in trades],
                     "positions": [{"id": p.id, "status": p.status, "entry_at": p.entry_at, "entry_price": p.entry_price,
                                    "quantity": p.remaining_quantity, "stop_loss": p.stop_loss, "last_price": p.last_price,
                                    "realized_pnl": p.realized_pnl, "exit_reason": p.exit_reason,
                                    "venue": (p.plan or {}).get("venue")} for p in positions]})


@router.get("/positions")
async def positions(chain: str | None = Query(None, pattern=CHAIN), status: str = Query("open", pattern="^(open|closed)$"),
                    limit: int = Query(100, ge=1, le=500), db: AsyncSession = Depends(get_db),
                    _: str = Depends(get_current_username)) -> dict:
    engines = [f"evm_{chain}"] if chain else ["evm_bsc", "evm_robinhood"]
    rows = (await db.execute(select(PaperPosition).where(PaperPosition.engine.in_(engines), PaperPosition.status == status)
                             .order_by(desc(PaperPosition.entry_at)).limit(limit))).scalars().all()
    accounts = (await db.execute(select(PaperAccount).where(PaperAccount.name.in_(engines)))).scalars().all()
    out = []
    for p in rows:
        rem = p.remaining_quantity if p.remaining_quantity is not None else p.quantity
        value = (p.last_price or p.entry_price) * rem
        cost_open = (p.entry_cost_quote or 0) * (rem / (p.initial_quantity or p.quantity)) if (p.initial_quantity or p.quantity) else 0
        out.append({"id": p.id, "chain": p.engine.removeprefix("evm_"), "token": p.asset_id, "symbol": p.symbol,
                    "status": p.status, "entry_at": p.entry_at, "entry_price": p.entry_price, "quantity": rem,
                    "entry_cost": p.entry_cost_quote, "last_price": p.last_price, "last_marked_at": p.last_marked_at,
                    "stop_loss": p.stop_loss, "trailing_stop": p.trailing_stop, "tp_hits": p.tp_hits,
                    "unrealized_pnl": (value - cost_open) if p.status == "open" else None,
                    "realized_pnl": p.realized_pnl, "exit_reason": p.exit_reason, "exit_at": p.exit_at,
                    "venue": (p.plan or {}).get("venue"), "category": (p.plan or {}).get("category"),
                    "pnl_basis": "marked at the executable sell quote of the remaining tokens (fees and taxes included)"})
    return jsonable({"positions": out, "accounts": [{"name": a.name, "currency": a.quote_currency, "cash": a.cash_balance,
                                                     "starting": a.starting_balance} for a in accounts],
                     "mode": "PAPER"})


@router.get("/settings")
async def get_settings_(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    return {"settings": (await evm_settings.load(db)).to_dict(), "defaults": evm_settings.EvmTradingSettings().to_dict(),
            "note": "amounts are per chain in BNB (BSC) / ETH (Robinhood Chain); percentages come from the shared risk settings"}


@router.put("/settings")
async def put_settings(body: dict, request: Request, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                       username: str = Depends(get_current_username)) -> dict:
    current = await db.get(PlatformSetting, evm_settings.KEY)
    merged = {**(dict(current.value) if current else {}), **body}
    for c in ("bsc", "robinhood"):  # per-chain objects merge key by key
        if isinstance(body.get(c), dict) and current and isinstance(current.value.get(c), dict):
            merged[c] = {**current.value[c], **body[c]}
    s, errors = evm_settings.parse(merged)
    if errors:
        raise HTTPException(422, {"errors": errors})
    value = s.to_dict()
    await db.execute(insert(PlatformSetting).values(key=evm_settings.KEY, value=value).on_conflict_do_update(
        index_elements=["key"], set_={"value": value, "updated_at": func.now()}))
    await audit(db, username, request, "evm_settings.update", {"changes": body})
    await db.commit()
    await events.publish(redis, "settings.updated", {"key": evm_settings.KEY}, "api")
    return {"settings": value}


@router.get("/wallet")
async def wallet(settings: Settings = Depends(get_settings), db: AsyncSession = Depends(get_db),
                 _: str = Depends(get_current_username)) -> dict:
    """The EVM half of the trading wallet: public address, whether a key is
    configured (never the key), native balances read from each chain now,
    and the EVM paper accounts, never mixed with LIVE."""
    acct = evm_wallet.account(settings)
    balances = {}
    if acct.get("address"):
        rpcs = {c: make_rpc(c, settings) for c in ("bsc", "robinhood")}
        try:
            balances = await evm_wallet.native_balances(acct["address"], rpcs)
        finally:
            for r in rpcs.values():
                await r.aclose()
    papers = (await db.execute(select(PaperAccount).where(PaperAccount.name.like("evm_%")))).scalars().all()
    return jsonable({"live": {**acct, "balances": balances, "currency": {"bsc": "BNB", "robinhood": "ETH"},
                              "execution": "EVM LIVE execution is not implemented; the address is watch-only"},
                     "paper": [{"name": a.name, "currency": a.quote_currency, "cash": a.cash_balance,
                                "starting": a.starting_balance} for a in papers]})
