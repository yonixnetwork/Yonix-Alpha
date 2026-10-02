"""BNB/USD and ETH/USD for USD market caps and balances (master §56, §61).

An executable price, not an index, like SOL/USD (solana/sol_price.py):
PancakeSwap V2 getAmountsOut of 1 WBNB -> USDT and of 1 Binance-Peg ETH ->
USDT on BSC. Robinhood Chain's native coin is ETH, so its USD rate is the
ETH rate. Refreshed by data-evm's BSC worker about once a minute and cached
in Redis; a reading older than MAX_AGE_SECONDS is not used, and a quote
outside SANE_RANGE is a broken response, not a price. Without a fresh rate
the caller shows native amounts only and never guesses.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from yonixalpha_core.chains.evm import dex
from yonixalpha_core.chains.registry import BSC_PANCAKE_V2, BSC_WBNB

BSC_USDT = "0x55d398326f99059fF775485246999027B3197955"  # Binance-Peg BSC-USD, 18 decimals
BSC_ETH = "0x2170Ed0880ac9A755fd29B2688956BD959F933F8"  # Binance-Peg Ethereum, 18 decimals
PAIRS = {"BNB": BSC_WBNB, "ETH": BSC_ETH}
CHAIN_SYMBOL = {"bsc": "BNB", "robinhood": "ETH"}
CACHE_KEY = "yx:native_usd:{symbol}"
CACHE_TTL = 300
MAX_AGE_SECONDS = 300
REFRESH_SECONDS = 60
SANE_RANGE = (Decimal("10"), Decimal("100000"))
E18 = 10 ** 18


async def refresh(bsc_rpc, redis, now: datetime) -> dict[str, Any]:
    """Quotes both rates and caches the sane ones. Returns {symbol: price or error}."""
    out: dict[str, Any] = {}
    for symbol, token in PAIRS.items():
        src = f"pancakeswap_v2.getAmountsOut(1 {symbol} -> USDT)"
        q = await dex.v2_quote(bsc_rpc, BSC_PANCAKE_V2["router"], [token, BSC_USDT], E18, src)
        price = Decimal(q.amount_out) / Decimal(E18) if q.ok and q.amount_out else None
        if price is None or not SANE_RANGE[0] <= price <= SANE_RANGE[1]:
            out[symbol] = {"error": q.error or f"price {price} outside {SANE_RANGE}"}
            continue
        await redis.set(CACHE_KEY.format(symbol=symbol),
                        json.dumps({"price": str(price), "source": src, "at": now.isoformat()}), ex=CACHE_TTL)
        out[symbol] = {"price": str(price)}
    return out


async def usd_rate(redis, chain_or_symbol: str, now: datetime) -> dict[str, Any]:
    """{"price", "source", "at", "age_s"} for a chain (bsc / robinhood) or a
    symbol (BNB / ETH); price None (with the reason) when no fresh rate."""
    symbol = CHAIN_SYMBOL.get(chain_or_symbol, chain_or_symbol)
    raw = await redis.get(CACHE_KEY.format(symbol=symbol)) if redis is not None else None
    if raw:
        try:
            c = json.loads(raw)
            age = (now - datetime.fromisoformat(c["at"])).total_seconds()
            if age <= MAX_AGE_SECONDS:
                return {"symbol": symbol, "price": c["price"], "source": c["source"], "at": c["at"], "age_s": round(age, 1)}
        except (ValueError, KeyError, TypeError):
            pass
    return {"symbol": symbol, "price": None, "source": None, "at": None, "age_s": None,
            "reason": f"no {symbol}/USD quote in the last {MAX_AGE_SECONDS // 60} minutes"}
