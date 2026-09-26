"""SOL/USD for the migrated-liquidity USD rule.

Primary: a Jupiter quote of 1 SOL -> USDC (an executable price, not an
index). Fallback: DexScreener's own pair for the token, whose priceUsd /
priceNative is the SOL/USD rate it used. Cached in Redis for a minute. If
neither answers, the price is unknown and the caller must not guess.
"""

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

from redis.asyncio import Redis

from yonixalpha_core.solana.pumpfun import WSOL_MINT

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
CACHE_KEY = "yx:sol_usd"
CACHE_TTL = 60
# A quote outside this band is a broken response, not a price.
SANE_RANGE = (Decimal("1"), Decimal("100000"))


def _sane(price: Decimal | None) -> bool:
    return price is not None and SANE_RANGE[0] <= price <= SANE_RANGE[1]


async def store(redis: Redis, price: Decimal, source: str, now: datetime, ttl: int = CACHE_TTL) -> None:
    await redis.set(CACHE_KEY, json.dumps({"price": str(price), "source": source, "at": now.isoformat()}), ex=ttl)


async def sol_usd(redis: Redis, jupiter, dexscreener, mint: str | None, now: datetime) -> tuple[Decimal | None, str, list[str]]:
    """(price, source, errors). price is None when no source answered."""
    errors: list[str] = []
    raw = await redis.get(CACHE_KEY)
    if raw:
        try:
            c = json.loads(raw)
            return Decimal(c["price"]), f"{c['source']} (cached {c['at']})", errors
        except (ValueError, KeyError, InvalidOperation):
            pass
    if jupiter is not None:
        try:
            q = await jupiter.quote(WSOL_MINT, USDC_MINT, 1_000_000_000, 50)
            price = Decimal(q.out_amount) / Decimal(10**6) if q.out_amount else None
            if _sane(price):
                await store(redis, price, "Jupiter quote 1 SOL -> USDC", now)
                return price, "Jupiter quote 1 SOL -> USDC", errors
            errors.append(f"jupiter SOL/USDC: {q.error or q.status}")
        except Exception as exc:  # noqa: BLE001 - a failed source is recorded, never guessed around
            errors.append(f"jupiter SOL/USDC: {type(exc).__name__}")
    if dexscreener is not None and mint:
        try:
            pool, err = await dexscreener.pool(mint)
            if pool is not None and pool.price_usd and pool.price_sol:
                price = pool.price_usd / pool.price_sol
                if _sane(price):
                    await store(redis, price, "DexScreener priceUsd / priceNative", now)
                    return price, "DexScreener priceUsd / priceNative", errors
            errors.append(f"dexscreener SOL/USD: {err or 'no SOL pair with a USD price'}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"dexscreener SOL/USD: {type(exc).__name__}")
    return None, "unavailable", errors
