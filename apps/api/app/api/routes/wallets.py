"""The YonixAlpha Trading Wallet across Solana, BSC and Robinhood Chain
(master §56-58): Total / Available / Reserved / Gas reserve / Trading
balance per chain, LIVE and PAPER side by side and never mixed. Public
addresses only; a private key never reaches this module."""

import json
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy import Numeric, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.api.util import jsonable
from yonixalpha_core import balances, live_trading
from yonixalpha_core.chains.evm import native_price, paper as evm_paper
from yonixalpha_core.chains.evm import settings as evm_settings
from yonixalpha_core.chains.evm import wallet as evm_wallet
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import ExecutionOrder, PaperPosition
from yonixalpha_core.safety import store
from yonixalpha_core.solana import sol_price

router = APIRouter(prefix="/wallets", tags=["wallets"])
EVM_WALLET_KEY = "yx:evm:wallet:{chain}"  # written by data-evm (worker.WALLET_KEY)
SOL_USD_MAX_AGE_SECONDS = 300


async def _json(redis: Redis, key: str) -> dict | None:
    raw = await redis.get(key)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


async def _sol_usd(redis: Redis, now: datetime) -> str | None:
    c = await _json(redis, sol_price.CACHE_KEY)
    if c and (now - datetime.fromisoformat(c["at"])).total_seconds() <= SOL_USD_MAX_AGE_SECONDS:
        return c["price"]
    return None


async def _open_value(db: AsyncSession, account_id) -> Decimal:
    rows = (await db.execute(select(PaperPosition).where(PaperPosition.account_id == account_id,
                                                         PaperPosition.status == "open"))).scalars().all()
    return sum((store.marked_value(p) for p in rows), Decimal(0))


@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                   settings: Settings = Depends(get_settings), _: str = Depends(get_current_username)) -> dict:
    now = datetime.now(timezone.utc)
    rates = {"solana": await _sol_usd(redis, now)}
    for c in ("bsc", "robinhood"):
        rates[c] = (await native_price.usd_rate(redis, c, now))["price"]
    live_cfg = await live_trading.load_live_settings(db)
    pending = (await db.execute(select(func.coalesce(func.sum(cast(ExecutionOrder.amount, Numeric)), 0)).where(
        ExecutionOrder.mode == "LIVE", ExecutionOrder.side == "BUY",
        ExecutionOrder.status.in_(("PENDING", "SIGNED", "SUBMITTED"))))).scalar_one()
    evm_cfg = await evm_settings.load(db)
    acct = evm_wallet.account(settings)
    evm_address = acct.get("address") if acct.get("status") in ("OK", "WATCH_ONLY") else None

    rows = [balances.solana_live(await _json(redis, live_trading.WALLET_KEY), live_cfg.min_sol_reserve,
                                 Decimal(pending), now, rates["solana"])]
    sol_paper = await store.get_paper_account(db, "solana")
    rows.append(balances.paper("solana", sol_paper.cash_balance, await _open_value(db, sol_paper.id), None,
                               rates["solana"], "no fee reserve is held back in Solana paper (the reserve applies to "
                                                "the LIVE wallet, which pays the fees)"))
    for c in ("bsc", "robinhood"):
        cs = evm_cfg.chain(c)
        rows.append(balances.evm_live(c, evm_address, await _json(redis, EVM_WALLET_KEY.format(chain=c)),
                                      cs.gas_reserve, now, rates[c]))
        pa = await evm_paper.ensure_account(db, c)
        rows.append(balances.paper(c, pa.cash_balance, await _open_value(db, pa.id), cs.gas_reserve, rates[c],
                                   "gas reserve held back on every paper entry, as LIVE would"))
    await db.commit()  # ensure_account may have created a paper book
    return jsonable({
        "wallet": "YonixAlpha Trading Wallet",
        "accounts": {"solana": {"chains": ["solana"], "key": "ed25519 (WALLET_PRIVATE_KEY)", "address":
                                (await _json(redis, live_trading.WALLET_KEY) or {}).get("pubkey")},
                     "evm": {"chains": ["bsc", "robinhood"], "key": "secp256k1 (EVM_WALLET_PRIVATE_KEY, optional)",
                             "address": evm_address, "status": acct.get("status")}},
        "rows": [r.to_dict() for r in rows],
        "usd_rates": {c: {"price": rates[c]} for c in rates},
        "definitions": {"total": "native coin held (paper: cash + open positions at their mark)",
                        "reserved": "committed, not spendable (LIVE: pending buy orders; PAPER: open positions)",
                        "available": "total - reserved", "gas_reserve": "kept back for transaction fees, never traded",
                        "trading_balance": "available - gas reserve: what a new entry may use"},
        "note": "The Solana and EVM accounts have separate keys; neither is derived from the other. Only public "
                "addresses are shown. EVM LIVE execution is locked (watch-only).",
    })
