"""Uniswap V3 pools announced by a launchpad (instant launches, and curve
tokens after migration) on Robinhood Chain: Swap events → trades, QuoterV2
quotes, pool price. Only pools the launchpad itself announced are tracked,
and only pools paired with WETH (native ETH) are quoted in this phase.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from yonixalpha_core.chains.base import Quote, TokenCategory, TokenState
from yonixalpha_core.chains.evm import dex
from yonixalpha_core.chains.evm.launchpad import ScanResult
from yonixalpha_core.chains.registry import ROBINHOOD_UNISWAP_V3, ROBINHOOD_WETH


class V3PoolsMixin:
    """Needs `self.rpc`, `self.spec`, `self._trade`. Keeps
    pool -> {"token", "quote", "fee"} for pools this launchpad announced."""

    def _pools_init(self) -> None:
        self.pools: dict[str, dict[str, Any]] = {}
        self.token_pool: dict[str, str] = {}

    def register_pool(self, pool: str, token: str, quote: str | None = ROBINHOOD_WETH) -> None:
        self.pools[pool.lower()] = {"pool": pool, "token": token, "quote": quote, "fee": None}
        self.token_pool[token.lower()] = pool

    async def _pool_meta(self, pool: str) -> dict[str, Any]:
        meta = self.pools[pool.lower()]
        if meta["quote"] is None:
            t0 = (await dex.call(self.rpc, pool, "token0()", ["address"]))[0]
            t1 = (await dex.call(self.rpc, pool, "token1()", ["address"]))[0]
            meta["quote"] = t1 if t0.lower() == meta["token"].lower() else t0
        if meta["fee"] is None:
            meta["fee"] = await dex.v3_pool_fee(self.rpc, pool)
        return meta

    async def _handle_swap(self, a: dict[str, Any], log: dict[str, Any], at: datetime, res: ScanResult) -> None:
        meta = self.pools.get((log.get("address") or "").lower())
        if meta is None:
            res.rejected_foreign += 1
            return
        if meta["quote"] is None:
            await self._pool_meta(meta["pool"])
        t = dex.v3_swap_to_trade(a, meta["token"], meta["quote"])
        if t is None:
            return
        res.trades.append(self._trade(
            log, at, token=meta["token"], trader=a["recipient"], is_buy=t["is_buy"], token_amount=t["token_amount"],
            quote_amount=t["quote_amount"], price=t["price"],
            extra={"venue": "uniswap_v3", "pool": meta["pool"], "router_or_sender": a["sender"],
                   "quote_token": meta["quote"], "native_quote": meta["quote"].lower() == ROBINHOOD_WETH.lower(),
                   "trader_note": "recipient of the swap; the signer is resolved from the receipt when needed"}))

    async def v3_quote(self, token: str, amount: int, buy: bool) -> Quote:
        src = "uniswap_v3.QuoterV2"
        pool = self.token_pool.get(token.lower())
        if pool is None:
            return dex.failed(amount, src, "no Uniswap V3 pool known for this token")
        try:
            meta = await self._pool_meta(pool)
        except Exception as exc:  # noqa: BLE001
            return dex.failed(amount, src, exc)
        if meta["quote"].lower() != ROBINHOOD_WETH.lower():
            return dex.failed(amount, src, f"pool pairs {meta['quote']}, not WETH; unsupported in this phase")
        tin, tout = (ROBINHOOD_WETH, token) if buy else (token, ROBINHOOD_WETH)
        return await dex.v3_quote(self.rpc, ROBINHOOD_UNISWAP_V3["quoter_v2"], tin, tout, amount, meta["fee"], src)

    async def v3_state(self, token: str, category: TokenCategory) -> TokenState:
        pool = self.token_pool.get(token.lower())
        if pool is None:
            raise LookupError(f"no Uniswap V3 pool known for {token}")
        meta = await self._pool_meta(pool)
        sqrt_p, *_ = await dex.call(self.rpc, pool, "slot0()",
                                    ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"])
        token_is_0 = dex.v3_token0(token, meta["quote"]).lower() == token.lower()
        quote_bal = await dex.erc20_balance(self.rpc, meta["quote"], pool)
        return TokenState(chain=self.spec.chain, launchpad=self.spec.key, token=token, category=category, stage="DEX",
                          price=dex.sqrt_price_to_price(sqrt_p, token_is_0), liquidity_quote=Decimal(quote_bal) / 10 ** 18,
                          progress=None, pool=pool, buy_tax_bps=None, sell_tax_bps=None,
                          source="uniswap_v3 pool slot0 + quote balance", at=dex.now(),
                          extra={"fee": meta["fee"], "quote_token": meta["quote"],
                                 "tax_note": "token transfer taxes are measured by the sellability round trip, not here"})
