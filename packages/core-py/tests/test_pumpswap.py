"""PumpSwap (migrated Pump.fun tokens): addresses checked against real
mainnet data from the official docs, decoding against the official IDL
layout, pool verification, and the migrated path through the safety gate."""

import base64
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from redis.asyncio import from_url

from yonixalpha_core.safety.gate import assess
from yonixalpha_core.safety.models import FinalDecision
from yonixalpha_core.safety.settings import default_settings_for
from yonixalpha_core.solana import pumpswap
from yonixalpha_core.solana.codec import TruncatedData
from yonixalpha_core.solana.assembler import Controls, Sources, assemble_migrated
from yonixalpha_core.solana.txguard import TOKEN, TOKEN_2022, ata
from yonixalpha_core.testing.pump import CREATOR, MINT, FakeRpc, empty_account, wallet
from yonixalpha_core.testing.pumpswap import FakePoolRpc, pool_account, trade_event, trade_history

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
MIGRATED = default_settings_for("solana_migration")

# Real mainnet pool from pump-public-docs docs/PUMP_SWAP_README.md.
DOC_MINT = "7LSsEoJGhLeZzGvDofTdNg7M3JttxQqGWNLo6vWMpump"
DOC_POOL = "GseMAnNDvntR5uFePZ51yZBXzNSn7GdFPkfHwfr6d77J"
DOC_POOL_AUTHORITY = "9XDYTfQKwW8sHPqnFdUreMmtmffmkHVPGTNV2e3LKxNW"
DOC_BASE_VAULT = "5jMpkf4JF4noHftLgNKyPNh6roVfPSGSjuEk3U4eLKRa"
DOC_QUOTE_VAULT = "43DVcZR4kQFjh4Xm2i3DcneRxNjZp7HMud8yDrJWrDr8"


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/9"), decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


def test_addresses_match_the_documented_mainnet_pool():
    assert pumpswap.pool_authority(DOC_MINT) == DOC_POOL_AUTHORITY
    assert pumpswap.canonical_pool(DOC_MINT) == DOC_POOL
    assert ata(DOC_POOL, DOC_MINT, TOKEN) == DOC_BASE_VAULT and ata(DOC_POOL, DOC_MINT, TOKEN_2022) != DOC_BASE_VAULT
    assert ata(DOC_POOL, pumpswap.WSOL_MINT, TOKEN) == DOC_QUOTE_VAULT


def test_pool_account_with_appended_fields_decodes():
    acct = pumpswap.decode_pool(pool_account(DOC_MINT, DOC_BASE_VAULT, DOC_QUOTE_VAULT, virtual_quote=123))
    assert (acct.index, acct.creator, acct.base_mint, acct.quote_mint) == (0, DOC_POOL_AUTHORITY, DOC_MINT, pumpswap.WSOL_MINT)
    assert (acct.base_vault, acct.quote_vault, acct.virtual_quote_reserves) == (DOC_BASE_VAULT, DOC_QUOTE_VAULT, 123)
    with pytest.raises(Exception):
        pumpswap.decode_pool(b"\x00" * 300)


@pytest.mark.parametrize("is_buy", [True, False])
def test_trade_event_decodes_with_the_fee_actually_paid(is_buy):
    ev = trade_event(DOC_POOL, wallet(3), NOW, is_buy, 5_000_000, 1_000_000_000, 10**15, 80 * 10**9, buyback_bps=5)
    t = pumpswap.decode_trade_event(ev)
    assert (t.is_buy, t.user, t.base_raw, t.pool) == (is_buy, wallet(3), 5_000_000, DOC_POOL)
    # lp 20 + protocol 5 + creator 5 declared; buyback 5 is only visible in the amounts.
    assert t.fee_bps == 35
    assert t.quote_lamports == (1_003_500_000 if is_buy else 996_500_000)
    with pytest.raises(TruncatedData):
        pumpswap.decode_trade_event(ev[:60])
    # A truncated or foreign event in the logs is skipped, never guessed.
    assert pumpswap.trades_from_logs(["Program data: " + base64.b64encode(ev[:60]).decode(),
                                      "Program data: " + base64.b64encode(b"x" * 40).decode()], DOC_POOL) == []


async def test_recent_trades_are_parsed_once_and_cached(redis):
    rpc = FakePoolRpc(MINT, 10**15, 80 * 10**9, trade_history(pumpswap.canonical_pool(MINT), 8, 2, NOW - timedelta(minutes=10)))
    first = await pumpswap.recent_pool_trades(rpc, redis, rpc.pool)
    n = rpc.calls.count("getTransaction")
    second = await pumpswap.recent_pool_trades(rpc, redis, rpc.pool)
    assert len(first) == len(second) == 10 and n == 10 and rpc.calls.count("getTransaction") == 10
    assert first == sorted(first, key=lambda t: t.at)


@pytest.mark.parametrize("kw, reason", [
    ({"exists": False}, "does not exist"),
    ({"owner": "11111111111111111111111111111111"}, "not owned by PumpSwap"),
    ({"pool_mint": wallet(77)}, "mints do not match"),
    ({"base_reserve": 0}, "no reserves"),
])
async def test_pool_that_cannot_be_verified_is_unavailable(kw, reason):
    args = {"base_reserve": 10**15, "quote_reserve": 80 * 10**9, "trades": [], **kw}
    rpc = FakePoolRpc(MINT, args.pop("base_reserve"), args.pop("quote_reserve"), args.pop("trades"), **args)
    with pytest.raises(pumpswap.PoolUnavailable, match=reason):
        await pumpswap.fetch_pool(rpc, MINT, NOW, 30, 6)


async def test_pool_state_prices_and_models_including_virtual_quote():
    rpc = FakePoolRpc(MINT, 10**15, 80 * 10**9, [])
    st = await pumpswap.fetch_pool(rpc, MINT, NOW, 30, 6)
    assert st.canonical and st.liquidity_sol == Decimal(80) and st.price == Decimal(80) / Decimal(10**9)
    assert st.model().fee_bps == 30
    with pytest.raises(pumpswap.PoolUnavailable, match="fee unknown"):
        (await pumpswap.fetch_pool(rpc, MINT, NOW, None, 6)).model()


async def test_migrated_token_is_assessed_from_its_pool(redis):
    pool = pumpswap.canonical_pool(MINT)
    # A pool that has traded for 30 min, with steady buying from many wallets in the last 10.
    history = trade_history(pool, 30, 0, NOW - timedelta(minutes=30), every=40, tag="old")[5:]
    rpc = FakePoolRpc(MINT, 700_000_000_000_000, 95 * 10**9,
                      trade_history(pool, 25, 0, NOW - timedelta(seconds=25 * 20), every=20) + history, inner=FakeRpc(None))
    inp, ev = await assemble_migrated(Sources(redis, rpc), MINT, NOW, Controls(MIGRATED, empty_account()))
    a = assess(inp, MIGRATED)
    assert ev["source"] == "PUMPFUN" and ev["lifecycle"] == "MIGRATED" and ev["pool"]["verified"] is True
    assert ev["pool"]["address"] == pool and ev["pool"]["fee_bps"] == 30
    assert inp.market.migrated and inp.liquidity_model is not None
    assert inp.flow.observation.source == "rpc:pumpswap_events" and inp.flow.unique_buyers > 0
    assert "NO_WALLET_DATA" not in {f.code for f in a.findings}  # wallet-level flow comes from pool events
    assert a.reports["sellability"]["route"] == "exact pool/curve simulation"
    assert a.reports["sellability"]["status"] == "SELLABLE"
    assert a.reports["tax"]["decision"] == "PASS"
    assert a.decision == FinalDecision.EXECUTE, a.reasons
    assert a.plan.complete and a.plan.max_loss.value > 0


async def test_graduated_token_without_its_pool_waits_for_migration(redis):
    rpc = FakePoolRpc(MINT, 10**15, 80 * 10**9, [], inner=FakeRpc(None), exists=False)
    inp, ev = await assemble_migrated(Sources(redis, rpc), MINT, NOW, Controls(MIGRATED, empty_account()))
    a = assess(inp, MIGRATED)
    assert ev["pool"]["verified"] is False and any("does not exist" in e for e in ev["errors"])
    assert not a.executable and "MIGRATION_PENDING" in {f.code for f in a.findings}


def test_creator_constant_is_distinct_from_pool_authority():
    assert CREATOR != pumpswap.pool_authority(MINT)
