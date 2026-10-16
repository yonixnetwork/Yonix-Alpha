"""The worker's pass over exchange-account trades (called by engine.Engine
on every loop while any exchange trade is not final):

1. reconcile every connection that has an active trade (after a restart
   this is the recovery: the exchange's state is read back first);
2. manage each OPEN trade on that connection's own market data: decisions
   run every loop (so a stop or an engine-managed target reacts within one
   loop), a HOLD is recorded once per closed bar.

A connection whose keys cannot be read, or whose exchange cannot be reached,
gets its new entries blocked and an ERROR event; nothing is guessed.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select

from yonixalpha_crypto.candles import INTERVAL_SECONDS
from yonixalpha_crypto.db import ApiConnection, ExecTrade
from yonixalpha_crypto.private import management, secretstore
from yonixalpha_crypto.private.adapters import SpotAccount, account_for
from yonixalpha_crypto.private.connections import view
from yonixalpha_crypto.private.executor import ACTIVE_TRADE, Executor
from yonixalpha_crypto.private.signedhttp import ExchangeRequestError, SignedHttp


def open_account(c: ApiConnection, *, transport: httpx.AsyncBaseTransport | None = None,
                 env: dict[str, str] | None = None) -> SpotAccount | None:
    key, secret = secretstore.decrypt(c.key_enc, env), secretstore.decrypt(c.secret_enc, env)
    if key is None or secret is None:
        return None
    return account_for(SignedHttp(c.exchange, c.environment, key, secret, connection=view(c), transport=transport))


async def run_exchange_accounts(sf, now: datetime, *, transport: httpx.AsyncBaseTransport | None = None,
                                env: dict[str, str] | None = None) -> dict[str, Any]:
    async with sf() as s:
        ids = select(ExecTrade.connection_id).where(ExecTrade.status.in_(ACTIVE_TRADE))
        conns = (await s.execute(select(ApiConnection).where(ApiConnection.id.in_(ids)))).scalars().all()
    out: dict[str, Any] = {}
    for c in conns:
        acct = open_account(c, transport=transport, env=env)
        if acct is None:
            async with sf() as s:
                ex = Executor(_NoAccount(c), c, clock=lambda: now)
                await ex.block(s, "keys cannot be decrypted: open trades cannot be reconciled")
                await s.commit()
            out[str(c.id)] = "keys unreadable"
            continue
        ex = Executor(acct, c)
        try:
            async with sf() as s:
                rec = await ex.reconcile(s)
            managed = 0
            async with sf() as s:
                trades = (await s.execute(select(ExecTrade).where(ExecTrade.connection_id == c.id,
                                                                  ExecTrade.status == "OPEN"))).scalars().all()
                for t in trades:
                    managed += await _manage_one(s, ex, acct, t, now)
            out[str(c.id)] = {"reconciled": rec, "managed": managed}
        except ExchangeRequestError as e:
            async with sf() as s:
                await ex.block(s, f"exchange unreachable while trades are open: {e}"[:400])
                await s.commit()
            out[str(c.id)] = f"error: {e}"[:300]
        finally:
            await acct.aclose()
    return out


async def _manage_one(s, ex: Executor, acct: SpotAccount, t: ExecTrade, now: datetime) -> int:
    interval = t.plan.get("interval", "5m")
    step = INTERVAL_SECONDS.get(interval, 300)
    rows = await acct.bars(t.symbol, interval, 60)
    closed = [r for r in rows if r[0] / 1000 + step <= now.timestamp()]
    book = await acct.book(t.symbol)
    last_bar = closed[-1][0] if closed else None
    new_bar = last_bar is not None and t.thesis.get("last_managed_bar") != last_bar
    held = sum(1 for r in closed if t.opened_at and r[0] / 1000 >= t.opened_at.timestamp())
    m = management.MarketSnapshot(book.bid, book.ask, [management.Bar(h, lo, cl) for _, h, lo, cl in closed])
    await ex.manage(s, t, m, held, Decimal(t.plan.get("taker_fee_bps", "10")), record_hold=new_bar)
    if new_bar and t.status == "OPEN":
        t.thesis = {**t.thesis, "last_managed_bar": last_bar}
        await s.commit()
    return 1


class _NoAccount:
    """Stands in for an account whose keys cannot be read, so the guard can
    still be set through the executor."""
    exchange_take_profit = False
    stop_amendable = False

    def __init__(self, c: ApiConnection):
        self.exchange, self.environment = c.exchange, c.environment
