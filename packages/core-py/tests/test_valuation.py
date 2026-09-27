"""Live wallet holdings valued from real sources only; anything without a
reliable price is VALUATION UNAVAILABLE, never estimated."""

import json
import os
from decimal import Decimal

import pytest_asyncio
from redis.asyncio import from_url

from yonixalpha_core.solana.pumpfun import WSOL_MINT
from yonixalpha_core.solana.sol_price import CACHE_KEY
from yonixalpha_core.solana.valuation import value_holdings
from yonixalpha_core.testing.pump import MINT, seed_healthy_launch, wallet

from tests.test_pump_pipeline import NOW

OTHER = wallet(77)


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/9"), decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


class NoPoolRpc:
    async def call(self, method, params=None):
        assert method == "getAccountInfo"
        return {"value": None}


async def test_curve_wsol_and_unvaluable_holdings(redis):
    curve = await seed_healthy_launch(redis, NOW)
    await redis.set(CACHE_KEY, json.dumps({"price": "150", "source": "Jupiter quote 1 SOL -> USDC", "at": NOW.isoformat()}))
    tokens = {MINT: {"amount": 1_000_000_000, "decimals": 6}, WSOL_MINT: {"amount": 20_000_000, "decimals": 9},
              OTHER: {"amount": 5_000_000, "decimals": 6}}
    v = await value_holdings(redis, NoPoolRpc(), tokens, NOW)
    by = {h["mint"]: h for h in v["holdings"]}
    c = by[MINT]
    assert c["status"] == "VALUED" and c["price_source"].startswith("pump.fun bonding curve")
    assert Decimal(c["price_sol"]) == curve.price() and Decimal(c["value_sol"]) == Decimal(1000) * curve.price()
    assert c["value_usd"] == str((Decimal(c["value_sol"]) * 150).quantize(Decimal("0.01")))
    assert by[WSOL_MINT]["value_sol"] == "0.02"
    u = by[OTHER]
    assert u["status"] == "VALUATION UNAVAILABLE" and u["price_sol"] is None and "does not exist" in u["reason"]
    assert v["unvalued_count"] == 1 and Decimal(v["total_sol"]) == Decimal(c["value_sol"]) + Decimal("0.02")


async def test_old_curve_price_is_marked_stale_and_left_out_of_the_total(redis):
    await seed_healthy_launch(redis, NOW)
    from datetime import timedelta

    v = await value_holdings(redis, None, {MINT: {"amount": 1_000_000, "decimals": 6}}, NOW + timedelta(hours=1))
    assert v["holdings"][0]["status"] == "STALE" and v["total_sol"] == "0" and v["unvalued_count"] == 1
    assert v["sol_usd"] is None
