"""Funding-link indicators end to end: stream -> assembler (fake RPC that
answers getSignaturesForAddress / getTransaction) -> safety gate.

Results are indicators (CREATOR-LINKED INDICATOR, RELATED-WALLET INDICATOR),
never accusations; exchange-like funders are ignored; an unavailable check
is reported, never read as "no links"."""

import os
from datetime import datetime, timezone

import pytest_asyncio
from redis.asyncio import from_url

from yonixalpha_core.safety.gate import assess
from yonixalpha_core.safety.models import FinalDecision, StrategyMode
from yonixalpha_core.safety.settings import default_settings_for
from yonixalpha_core.solana.assembler import Controls, Sources, assemble_fresh
from yonixalpha_core.testing.pump import CREATOR, MINT, FakeRpc, empty_account, seed_healthy_launch, wallet

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
FRESH = default_settings_for("solana_fresh")
HUB = wallet(90)


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/9"), decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


async def run(redis, strategy_mode=StrategyMode.AUTO, **rpc_kw):
    curve = await seed_healthy_launch(redis, NOW)
    inp, ev = await assemble_fresh(Sources(redis, FakeRpc(curve, **rpc_kw)), MINT, NOW, Controls(FRESH, empty_account()))
    inp.strategy_mode = strategy_mode
    return inp, ev, assess(inp, FRESH)


def codes(a):
    return {f.code for f in a.findings}


async def test_established_early_buyers_produce_no_indicator(redis):
    inp, ev, a = await run(redis)
    assert ev["funding"]["checked"] == FRESH.funding_check_wallets and ev["funding"]["errors"] == []
    assert inp.flow.creator_linked_buyers == 0 and inp.flow.related_wallet_groups == 0
    assert not codes(a) & {"CREATOR_LINKED", "RELATED_WALLETS", "FUNDING_UNCHECKED"}
    assert a.decision == FinalDecision.EXECUTE


async def test_buyer_funded_by_creator_is_a_creator_linked_indicator(redis):
    inp, ev, a = await run(redis, funders={wallet(1): CREATOR})
    assert inp.flow.creator_linked_buyers == 1
    f = next(f for f in a.findings if f.code == "CREATOR_LINKED")
    assert "CREATOR-LINKED INDICATOR" in f.message and f.action == FinalDecision.REQUIRE_MANUAL_APPROVAL
    assert a.decision == FinalDecision.NO_TRADE and "AUTO_NO_APPROVAL" in codes(a)  # AUTO never waits


async def test_three_fresh_buyers_sharing_a_funder_is_a_related_wallet_indicator(redis):
    inp, ev, a = await run(redis, strategy_mode=StrategyMode.MANUAL, funders={wallet(i): HUB for i in (0, 2, 4)})
    assert inp.flow.related_wallet_groups == 3
    f = next(f for f in a.findings if f.code == "RELATED_WALLETS")
    assert "RELATED-WALLET INDICATOR" in f.message
    assert a.decision == FinalDecision.REQUIRE_MANUAL_APPROVAL


async def test_exchange_like_funder_is_not_a_cluster(redis):
    inp, _, a = await run(redis, funders={wallet(i): HUB for i in (0, 2, 4)}, busy={HUB})
    assert inp.flow.related_wallet_groups == 0 and "RELATED_WALLETS" not in codes(a)


async def test_unavailable_funding_check_is_reported_not_assumed_clean(redis):
    inp, ev, a = await run(redis, fail={"getSignaturesForAddress"})
    assert ev["funding"]["checked"] == 0 and len(ev["funding"]["errors"]) == FRESH.funding_check_wallets
    assert inp.flow.creator_linked_buyers is None
    assert "FUNDING_UNCHECKED" in codes(a)


async def test_funding_cluster_reaches_the_relationship_graph_and_effective_buyers(redis):
    from yonixalpha_core.solana import funding as funding_mod, wallet_graph as wg
    _, ev, _ = await run(redis, strategy_mode=StrategyMode.MANUAL, funders={wallet(i): HUB for i in (0, 2, 4)})
    rel = ev["intel"]["relationships"]
    assert rel["status"] == "MEASURED" and rel["graph_version"] == wg.GRAPH_VERSION
    hub = next(c for c in rel["clusters"] if c["cluster_id"] == wg._cid("fund", HUB))
    assert hub["classification"] in (wg.FUNDING_RELATED, wg.COORDINATED) and hub["size"] == 3
    b = rel["buyers"]
    assert b["coordinated_buyers"] == 3 and b["effective_unique_buyers"] <= b["raw_unique_buyers"] - 2
    assert await redis.zcard(funding_mod.CHILDREN + HUB) == 3  # edges persisted for the next tokens
    assert "relationships" in ev["timings_ms"]


async def test_exchange_like_funder_leaves_buyers_independent_in_the_graph(redis):
    _, ev, _ = await run(redis, funders={wallet(i): HUB for i in (0, 2, 4)}, busy={HUB})
    rel = ev["intel"]["relationships"]
    assert rel["buyers"]["coordinated_buyers"] == 0 and rel["cluster_count"] == 0
