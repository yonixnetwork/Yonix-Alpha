"""Regression: decision-engine candidate a8fd3422… crashed every evaluation
with decimal.DivisionUndefined. Cause: assemble_fresh priced the bonding
curve read over RPC as virtual_sol / virtual_tokens; after `migrate` both are
0 (the official SDK reads virtual_token_reserves == 0 as a migrated curve),
so the price was 0/0. A curve without reserves has NO price: unavailable,
never zero and never a signal."""

import decimal
import os
from decimal import Decimal

import pytest
import pytest_asyncio
from redis.asyncio import from_url

from yonixalpha_core.safety.gate import assess
from yonixalpha_core.safety.settings import default_settings_for
from yonixalpha_core.solana.assembler import Controls, Sources, assemble_fresh
from yonixalpha_core.solana.pumpfun import decode_bonding_curve
from yonixalpha_core.testing.pump import MINT, FakeRpc, empty_account, migrated_curve_account, seed_healthy_launch

from tests.test_pump_pipeline import NOW

FRESH = default_settings_for("solana_fresh")
MOMENTUM = default_settings_for("solana_momentum")


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/9"), decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


def test_the_old_formula_was_zero_divided_by_zero():
    with pytest.raises(decimal.InvalidOperation) as e:
        (Decimal(0) / Decimal(1_000_000_000)) / (Decimal(0) / Decimal(10) ** 6)
    assert decimal.DivisionUndefined in e.value.args[0]


def test_migrated_curve_has_no_price_not_a_zero_price():
    state = decode_bonding_curve(migrated_curve_account())
    assert state.complete and not state.has_reserves
    assert state.price_sol(6) is None


@pytest.mark.parametrize("engine,settings", [("solana_fresh", FRESH), ("solana_momentum", MOMENTUM)])
async def test_migrated_curve_evaluates_deterministically_to_no_trade(redis, engine, settings):
    curve = await seed_healthy_launch(redis, NOW)
    rpc = FakeRpc(curve, curve_account=migrated_curve_account())
    c = Controls(settings=settings, account=empty_account())
    inp, ev = await assemble_fresh(Sources(redis, rpc), MINT, NOW, c, engine=engine)  # used to raise here
    assert inp.market is not None and inp.market.price is None and inp.market.curve_complete is True
    assert inp.liquidity_model is None and ev["curve_state"] == "MIGRATED_ON_CHAIN"
    assert any("no reserves" in e for e in ev["errors"])
    first = assess(inp, settings)
    again = assess(inp, settings)
    assert not first.executable and first.execution_target.value == "NONE"
    assert "PRICE_UNAVAILABLE" in {f.code for f in first.findings}
    assert (first.decision, sorted(f.code for f in first.findings)) == (again.decision, sorted(f.code for f in again.findings))


async def test_a_live_curve_is_still_priced_and_executable(redis):
    curve = await seed_healthy_launch(redis, NOW)
    inp, ev = await assemble_fresh(Sources(redis, FakeRpc(curve)), MINT, NOW, Controls(settings=FRESH, account=empty_account()))
    assert inp.market.price == curve.price() and inp.liquidity_model is not None and "curve_state" not in ev
    assert assess(inp, FRESH).executable
