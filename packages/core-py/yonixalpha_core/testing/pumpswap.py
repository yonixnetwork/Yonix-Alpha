"""Byte-exact PumpSwap fixtures, encoded from the official IDL layout
(pump-public-docs idl/pump_amm.json) independently of the decoder, with every
trailing field the current program emits, plus a fake RPC that serves a
canonical pool, its vaults and its recent trades."""

import base64
import struct
from datetime import datetime, timedelta
from decimal import Decimal

from yonixalpha_core.solana import pumpswap
from yonixalpha_core.testing.pump import SUPPLY, b, i64, pk, s, u64, wallet


def i128(v: int) -> bytes:
    return v.to_bytes(16, "little", signed=True)


def pool_account(mint: str, base_vault: str, quote_vault: str, virtual_quote: int = 0, coin_creator: str | None = None) -> bytes:
    creator = pumpswap.pool_authority(mint)
    return (pumpswap.POOL_DISC + bytes([254]) + struct.pack("<H", 0) + pk(creator) + pk(mint) + pk(pumpswap.WSOL_MINT)
            + pk(creator) + pk(base_vault) + pk(quote_vault) + u64(4_194_352_106_721) + pk(coin_creator or creator)
            + b(False) + b(False) + i128(virtual_quote) + u64(0) + b(False) + b(False))


def trade_event(pool: str, user: str, at: datetime, is_buy: bool, base: int, swap_quote: int, pool_base: int, pool_quote: int,
                lp_bps: int = 20, protocol_bps: int = 5, creator_bps: int = 5, buyback_bps: int = 0) -> bytes:
    """A BuyEvent/SellEvent. Fees are charged on top of a buy's swap amount
    and taken out of a sell's, as the program does."""
    fee = lambda bps: swap_quote * bps // 10_000  # noqa: E731
    total = fee(lp_bps) + fee(protocol_bps) + fee(creator_bps) + fee(buyback_bps)
    user_quote = swap_quote + total if is_buy else swap_quote - total
    head = (i64(int(at.timestamp())) + u64(base) + u64(user_quote) + u64(0) + u64(0) + u64(pool_base) + u64(pool_quote)
            + u64(swap_quote) + u64(lp_bps) + u64(fee(lp_bps)) + u64(protocol_bps) + u64(fee(protocol_bps))
            + u64(swap_quote + fee(lp_bps) if is_buy else swap_quote - fee(lp_bps)) + u64(user_quote)
            + pk(pool) + pk(user) + pk(user) + pk(user) + pk(pool) + pk(pool) + pk(pool) + u64(creator_bps) + u64(fee(creator_bps)))
    if is_buy:
        tail = (b(True) + u64(0) + u64(0) + u64(0) + i64(0) + u64(0) + s("buy") + u64(0) + u64(0) + u64(buyback_bps)
                + u64(fee(buyback_bps)) + i128(0) + b(False) + u64(10**15) + u64(0) + u64(0))
        return pumpswap.BUY_EVENT_DISC + head + tail
    tail = u64(0) + u64(0) + u64(buyback_bps) + u64(fee(buyback_bps)) + i128(0) + b(False) + u64(10**15) + u64(0) + u64(0)
    return pumpswap.SELL_EVENT_DISC + head + tail


def logs(*events: bytes) -> list[str]:
    return [f"Program {pumpswap.PUMP_AMM_PROGRAM} invoke [1]"] + [
        "Program data: " + base64.b64encode(e).decode() for e in events]


class FakePoolRpc:
    """Serves the canonical pool of `mint`: account, vault balances, and
    `trades` (list of (signature, event bytes)) newest first, as
    getSignaturesForAddress returns them. Other calls go to `inner`."""

    def __init__(self, mint: str, base_reserve: int, quote_reserve: int, trades: list[tuple[str, bytes]], inner=None,
                 owner: str = pumpswap.PUMP_AMM_PROGRAM, exists: bool = True, pool_mint: str | None = None):
        self.mint, self.base, self.quote, self.trades, self.inner = mint, base_reserve, quote_reserve, trades, inner
        self.owner, self.exists, self.pool_mint = owner, exists, pool_mint or mint
        self.pool = pumpswap.canonical_pool(mint)
        self.base_vault, self.quote_vault = wallet(150), wallet(151)
        self.calls: list[str] = []

    async def call(self, method, params=None):
        self.calls.append(method)
        if method == "getAccountInfo" and params[0] == self.pool:
            if not self.exists:
                return {"value": None}
            data = pool_account(self.pool_mint, self.base_vault, self.quote_vault)
            return {"value": {"owner": self.owner, "data": [base64.b64encode(data).decode(), "base64"]}}
        if method == "getMultipleAccounts" and params[0] == [self.base_vault, self.quote_vault]:
            return {"value": [{"data": {"parsed": {"info": {"tokenAmount": {"amount": str(a)}}}}} for a in (self.base, self.quote)]}
        if method == "getSignaturesForAddress" and params[0] == self.pool:
            return [{"signature": sig, "err": None} for sig, _ in self.trades][: params[1].get("limit", 25)]
        if method == "getTransaction" and any(sig == params[0] for sig, _ in self.trades):
            ev = next(e for sig, e in self.trades if sig == params[0])
            return {"meta": {"logMessages": logs(ev), "err": None}}
        if method == "getTokenLargestAccounts":
            # After migration the pool's base vault is the largest holder.
            accts = [{"address": self.base_vault, "amount": str(SUPPLY * 70 // 100)}]
            accts += [{"address": f"ta{i}", "amount": str(SUPPLY * 2 // 100)} for i in range(12)]
            return {"value": accts}
        if method == "getMultipleAccounts" and params[0] and params[0][0] == self.base_vault:
            owners = [self.pool] + [wallet(i) for i in range(len(params[0]) - 1)]
            return {"value": [{"data": {"parsed": {"info": {"owner": o}}}} for o in owners]}
        if self.inner is not None:
            return await self.inner.call(method, params)
        raise AssertionError(f"unexpected RPC {method}")


def trade_history(pool: str, n_buys: int, n_sells: int, start: datetime, every: int = 40, tag: str = "sig") -> list[tuple[str, bytes]]:
    """Consecutive trades of 0.3 SOL moving the pool's reserves, as
    (signature, event), newest first like getSignaturesForAddress."""
    out, base, quote = [], 800_000_000_000_000, 85 * 10**9
    for i in range(n_buys + n_sells):
        buy = i < n_buys
        sol = 300_000_000
        tok = base * sol // (quote + sol)
        base, quote = (base - tok, quote + sol) if buy else (base + tok, quote - sol)
        out.append((f"{tag}{i}", trade_event(pool, wallet(20 + i % 30), start + timedelta(seconds=every * i), buy, tok, sol, base, quote)))
    return list(reversed(out))  # newest first, like getSignaturesForAddress


SOL_USD = Decimal("150")


async def seed_sol_usd(redis, now: datetime, price: Decimal = SOL_USD) -> None:
    """Puts a SOL/USD price in the cache the migrated-liquidity rule reads,
    so tests never call Jupiter or DexScreener."""
    from yonixalpha_core.solana import sol_price

    await sol_price.store(redis, price, "test fixture", now, ttl=3600)
