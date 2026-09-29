"""Wallet relationships, effective buyers, organic demand, flows, smart-money
context (solana.wallet_graph), the manufactured-pump detector
(solana.manufactured_pump) and observation coverage (solana.intel)."""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from redis.asyncio import from_url

from yonixalpha_core.solana import funding, manufactured_pump as mp, wallet_graph as wg
from yonixalpha_core.solana.creator_history import bonding_curve_pda
from yonixalpha_core.solana.flow import Trade
from yonixalpha_core.solana.intel import coverage_status

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
MINT = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
CREATOR = "Creator1111111111111111111111111111111111111"
HUB, EXCHANGE = "HubFunder1111111111111111111111111111111111", "Exchange11111111111111111111111111111111111"
SOL = 1_000_000_000


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/9"), decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


def tr(sec: float, who: str, buy: bool = True, sol: float = 1.0, vsol: int = 30 * SOL, vtok: int = 10**15) -> Trade:
    return Trade(NOW + timedelta(seconds=sec), who, buy, int(sol * SOL), 10**9, vsol, vtok)


def ctx(funding_map: dict, busy: dict | None = None, fanout: dict | None = None, creator_funding=None) -> dict:
    return {"funding": funding_map, "busy": busy or {}, "fanout": fanout or {}, "creator_funding": creator_funding}


FRESH = lambda f, at=None: {"fresh": True, "funder": f, "funded_at": at}  # noqa: E731
ESTABLISHED = {"fresh": False, "funder": None}


def launch() -> list[Trade]:
    """creator + 1 creator-funded wallet + 3 wallets funded by HUB + 2
    established wallets + 1 unchecked wallet; 1 SOL buys each."""
    return [tr(-30, CREATOR), tr(-29, "Linked"), tr(-20, "Hub1"), tr(-19, "Hub2"), tr(-18, "Hub3"),
            tr(-10, "Est1"), tr(-9, "Est2"), tr(-5, "Unknown1")]


FUNDING = {"Linked": FRESH(CREATOR), "Hub1": FRESH(HUB, 100), "Hub2": FRESH(HUB, 160), "Hub3": FRESH(HUB, 220),
           "Est1": ESTABLISHED, "Est2": ESTABLISHED}


def test_effective_buyers_collapse_a_funding_cluster_and_drop_creator_wallets():
    r = wg.analyse(launch(), NOW, ctx(FUNDING, busy={HUB: False}, fanout={HUB: 7}), creator=CREATOR, mint=MINT)
    b = r["buyers"]
    assert b["raw_unique_buyers"] == 8
    # 8 − 2 creator-related − (3 − 1) for the HUB cluster = 4 (Hub cluster as one, Est1, Est2, Unknown1)
    assert b["effective_unique_buyers"] == 4 and b["creator_related_buyers"] == 2 and b["coordinated_buyers"] == 3
    assert b["independent_buyers"] == 2 and b["unattributed_buyers"] == 1
    assert "counts as one" in b["explanation"] and b["explanation"].endswith("= 4 effective")
    clusters = {c["classification"]: c for c in r["clusters"]}
    hub = clusters[wg.FUNDING_RELATED]
    assert hub["cluster_id"] == wg._cid("fund", HUB) and hub["size"] == 3 and hub["parents"] == [HUB]
    assert hub["funding_fanout"] == {HUB: 7} and hub["funding_window_seconds"] == 120 and hub["evidence"]
    assert clusters[wg.CREATOR_RELATED]["members"] == sorted([CREATOR, "Linked"])


def test_same_second_activity_upgrades_a_funding_cluster_to_coordinated_but_timing_alone_never_links():
    trades = launch() + [tr(-2.2, "Hub1", buy=False), tr(-2.9, "Hub2", buy=False)]
    r = wg.analyse(trades, NOW, ctx(FUNDING, busy={HUB: False}), creator=CREATOR, mint=MINT)
    hub = next(c for c in r["clusters"] if c["cluster_id"] == wg._cid("fund", HUB))
    assert hub["classification"] == wg.COORDINATED and hub["coordinated_sell_seconds"] == 1
    # Two unrelated established wallets buying in the same second are not a cluster.
    solo = wg.analyse([tr(-5.2, "Est1"), tr(-5.3, "Est2")], NOW, ctx({"Est1": ESTABLISHED, "Est2": ESTABLISHED}),
                      creator=None, mint=MINT)
    assert solo["cluster_count"] == 0 and solo["buyers"]["effective_unique_buyers"] == 2


def test_exchange_like_or_unchecked_shared_funders_are_not_relationships():
    busy = wg.analyse(launch(), NOW, ctx(FUNDING, busy={HUB: True}), creator=CREATOR, mint=MINT)
    assert all(c["classification"] != wg.FUNDING_RELATED for c in busy["clusters"])
    assert busy["buyers"]["independent_buyers"] == 5  # funded from an exchange-like address: independent
    unchecked = wg.analyse(launch(), NOW, ctx(FUNDING, busy={}), creator=CREATOR, mint=MINT)
    assert unchecked["coverage"]["shared_funder_activity_unchecked"] == 3
    assert unchecked["buyers"]["unattributed_buyers"] == 4 and unchecked["buyers"]["effective_unique_buyers"] == 6


def test_organic_demand_is_bounded_and_single_valued_only_when_attributed():
    r = wg.analyse(launch(), NOW, ctx(FUNDING, busy={HUB: False}), creator=CREATOR, mint=MINT, min_attribution=0.5)
    d = r["demand"]
    assert d["total_volume_sol"] == 8.0 and d["known_related_volume_sol"] == 5.0 and d["unattributed_volume_sol"] == 1.0
    assert d["organic_demand_ratio_lower"] == 0.25 and d["organic_demand_ratio_upper"] == 0.375
    assert d["organic_demand_ratio"] == 0.25 and d["status"] == "MEASURED"
    none_known = wg.analyse(launch(), NOW, ctx({}), creator=None, mint=MINT)
    assert none_known["demand"]["organic_demand_ratio"] is None and none_known["demand"]["status"] == "UNATTRIBUTED"
    assert none_known["demand"]["organic_demand_ratio_lower"] == 0.0
    assert none_known["demand"]["organic_demand_ratio_upper"] == 1.0 and "not assumed organic" in none_known["demand"]["why"]


def test_creator_adjustment_keeps_raw_and_adjusted_volume():
    trades = launch() + [tr(-1, CREATOR, buy=False, sol=2.0)]
    c = wg.analyse(trades, NOW, ctx(FUNDING, busy={HUB: False}), creator=CREATOR, mint=MINT)["creator"]
    assert c["raw_volume_sol"] == 10.0 and c["adjusted_volume_sol"] == 3.0
    assert c["creator_sell_ratio"] == 1.0 and c["creator_buy_ratio"] == 0.125 and c["creator_volume_ratio"] == 0.3
    assert c["creator_related_buyer_ratio"] == 0.25 and "removed" in c["adjustment_reason"]


def test_flows_and_accelerations_separate_known_related_wallets():
    trades = [tr(-50, "Est1", sol=1), tr(-20, "Hub1", sol=3), tr(-15, "Hub2", sol=3), tr(-10, "Est2", sol=1),
              tr(-4, "Est1", buy=False, sol=0.5)]
    r = wg.analyse(trades, NOW, ctx({**FUNDING}, busy={HUB: False}), creator=CREATOR, mint=MINT)
    f30 = r["flows"]["30s"]
    assert f30["net_sol_flow"] == 6.5 and f30["cluster_buy_flow"] == 6.0 and f30["organic_net_sol_flow"] == 0.5
    a = r["acceleration"]
    assert a["raw_volume_acceleration"] == 7.5  # 7.5 SOL in the last 30 s vs 1 SOL before
    assert a["organic_volume_acceleration"] == 1.5  # without the HUB cluster: 1.5 vs 1
    assert a["raw_buyer_acceleration"] == 3.0 and a["effective_buyer_acceleration"] == 2.0


def test_smart_money_context_distinguishes_independent_from_related_wallets():
    smart = {"status": "MEASURED", "proven": [{"wallet": "Hub1", "lower": 0.3}, {"wallet": "Hub2", "lower": 0.5}]}
    rel = wg.analyse(launch(), NOW, ctx(FUNDING, busy={HUB: False}), creator=CREATOR, mint=MINT, smart=smart)["smart_money"]
    assert rel["context"] == "MULTIPLE_RELATED" and rel["smart_money_clustered"] == 2
    assert rel["smart_money_signal_strength"] == 1 and rel["smart_money_quality"] == 0.4
    smart2 = {"status": "MEASURED", "proven": [{"wallet": "Est1", "lower": 0.3}, {"wallet": "Est2", "lower": 0.3}]}
    ind = wg.analyse(launch(), NOW, ctx(FUNDING, busy={HUB: False}), creator=CREATOR, mint=MINT, smart=smart2)["smart_money"]
    assert ind["context"] == "MULTIPLE_INDEPENDENT" and ind["smart_money_signal_strength"] == 2
    assert "never a BUY trigger" in ind["note"]
    unk = wg.analyse(launch(), NOW, ctx(FUNDING), creator=CREATOR, mint=MINT, smart={"status": "UNKNOWN"})["smart_money"]
    assert unk["context"] == "UNKNOWN"


def test_co_dump_history_alone_is_potential_coordination_not_subtracted():
    r = wg.analyse(launch(), NOW, ctx({}), creator=None, mint=MINT, cohorts=[["Est1", "Est2", "NotHere"]])
    c = r["clusters"][0]
    assert c["classification"] == wg.POTENTIAL_COORDINATION and c["size"] == 2
    assert r["buyers"]["effective_unique_buyers"] == 8 and r["buyers"]["potential_coordination_buyers"] == 2


def test_protocol_accounts_are_never_creator_wallets():
    pda = bonding_curve_pda(MINT)
    r = wg.analyse([tr(-5, pda), tr(-4, "Est1")], NOW, ctx({pda: FRESH(CREATOR), "Est1": ESTABLISHED}),
                   creator=CREATOR, mint=MINT)
    roles = {w["wallet"]: w["role"] for w in r["wallets"]}
    assert roles[pda] == wg.PROTOCOL and r["buyers"]["protocol_buyers"] == 1 and r["buyers"]["creator_related_buyers"] == 0


def test_analysis_is_causal():
    early = wg.analyse(launch(), NOW - timedelta(seconds=15), ctx(FUNDING, busy={HUB: False}), creator=CREATOR, mint=MINT)
    assert early["buyers"]["raw_unique_buyers"] == 5  # Est1, Est2, Unknown1 bought after the decision time
    assert wg.analyse([], NOW, ctx({}), creator=None, mint=MINT)["status"] == "UNAVAILABLE"


async def test_graph_persists_funder_edges_and_decision_time_reads_cache_only(redis):
    for w, at in (("Hub1", 100), ("Hub2", 160), ("Other", 170)):
        await funding.record_child(redis, HUB, w, at)
        await redis.set(funding.funder_key(w), json.dumps(FRESH(HUB, at)))
    await redis.set(funding.busy_key(HUB), json.dumps({"busy": False}))
    c = await wg.gather(redis, ["Hub1", "Hub2", "Never"], CREATOR)
    assert c["funding"]["Never"] is None and c["busy"] == {HUB: False} and c["fanout"] == {HUB: 3}
    r = wg.analyse([tr(-5, "Hub1"), tr(-4, "Hub2"), tr(-3, "Never")], NOW, c, creator=CREATOR, mint=MINT)
    assert r["buyers"]["effective_unique_buyers"] == 2 and r["clusters"][0]["funding_fanout"] == {HUB: 3}


# --- manufactured pump -----------------------------------------------------------------------

def ramp(n: int, step: float = 0.02, every: float = 5.0, start: float = -300) -> list[Trade]:
    """A steady rise: every trade lifts the price by `step`, buys only."""
    out, v = [], 30 * SOL
    for i in range(n):
        v = int(v * (1 + step))
        out.append(tr(start + i * every, f"w{i}", True, 0.5, vsol=v, vtok=10**15))
    return out


def test_detector_flags_a_steady_manufactured_ramp():
    r = mp.detect(ramp(60), NOW)
    assert r["risk"] == "HIGH" and r["score"] == 1.0 and r["pattern_duration_seconds"] >= 120
    assert r["detector_version"] == mp.DETECTOR_VERSION and r["thresholds"]["min_log_price_r2"] == 0.85
    assert all(r["conditions"].values())


def test_detector_does_not_flag_a_choppy_market_and_needs_a_developed_pattern():
    choppy, v = [], 30 * SOL
    for i in range(60):
        v = int(v * (1.08 if i % 2 == 0 else 0.93))
        choppy.append(tr(-300 + i * 5, f"w{i}", i % 2 == 0, 0.5, vsol=v))
    assert mp.detect(choppy, NOW)["risk"] in ("LOW", "ELEVATED")
    young = mp.detect(ramp(4, start=-20), NOW)
    assert young["risk"] == "UNKNOWN" and "not an early-entry signal" in young["reason"]


def test_detector_is_causal():
    trades = ramp(30, start=-300) + ramp(30, step=0.02, start=0)  # the rise after NOW must not count
    before = mp.detect(trades, NOW - timedelta(seconds=160))
    assert before == mp.detect([t for t in trades if t.at <= NOW - timedelta(seconds=160)], NOW - timedelta(seconds=160))


def test_coverage_status():
    hb = NOW - timedelta(seconds=5)
    trades = [tr(-10, "a"), tr(-2, "b")]
    assert coverage_status(trades, NOW, {"complete": True}, created_ts=None, stream_heartbeat=hb)["coverage_status"] == "COMPLETE"
    p = coverage_status(trades, NOW, {"complete": False, "reason": "stream started after the token was created"},
                        created_ts=None, stream_heartbeat=hb)
    assert p["coverage_status"] == "PARTIAL" and "stream started" in p["reason"] and p["largest_trade_gap_seconds"] == 8
    assert coverage_status(trades, NOW, {"complete": True}, created_ts=None,
                           stream_heartbeat=NOW - timedelta(minutes=5))["coverage_status"] == "STALE"
    assert coverage_status([], NOW, None, created_ts=None, stream_heartbeat=hb)["coverage_status"] == "UNAVAILABLE"
