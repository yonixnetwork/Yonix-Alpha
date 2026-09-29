"""EVM safety, settings, trade stats and categories (fake node, no network)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx

from yonixalpha_core.chains.base import TokenCategory
from yonixalpha_core.chains.evm import safety
from yonixalpha_core.chains.evm import settings as evm_settings
from yonixalpha_core.chains.evm.abi import ZERO_ADDRESS, selector
from yonixalpha_core.chains.evm.fourmeme import FourMeme
from yonixalpha_core.chains.evm.rpc import EvmRpc
from yonixalpha_core.chains.evm.store import categorize, trade_stats
from yonixalpha_core.testing.evm_node import Node, enc, rpc_for

TOKEN = "0x1111111111111111111111111111111111111111"
S = evm_settings.EvmTradingSettings()


def four_node(sell_funds=9 * 10 ** 15):
    node = Node(56)
    lp = FourMeme(rpc_for(node))
    node.on(lp.helper, "getTokenInfo(address)", enc(
        ["uint256", "address", "address"] + ["uint256"] * 8 + ["bool"],
        [2, lp.spec.contracts["manager_v2"], ZERO_ADDRESS, 1, 100, 0, 1, 1, 1, 2 * 10 ** 18, 24 * 10 ** 18, False]))
    node.on(lp.helper, "tryBuy(address,uint256,uint256)",
            enc(["address", "address"] + ["uint256"] * 6, [TOKEN, ZERO_ADDRESS, 10 ** 22, 10 ** 16, 10 ** 14, 0, 0, 0]))
    node.on(lp.helper, "trySell(address,uint256)",
            enc(["address", "address", "uint256", "uint256"], [TOKEN, ZERO_ADDRESS, sell_funds, 10 ** 14]))
    return node, lp


async def test_safety_passes_only_with_a_sellable_round_trip_and_clean_contract():
    node, lp = four_node()
    report, rt = await safety.check(lp, TOKEN, 10 ** 16, S)
    assert report.verdict == "PASS", report.findings
    assert rt["sellable"] is True and rt["round_trip_loss_bps"] == 1000

    node.storage[(TOKEN.lower(), safety.EIP1967_IMPLEMENTATION)] = "0x" + "00" * 12 + "22" * 20
    report, _ = await safety.check(lp, TOKEN, 10 ** 16, S)
    assert report.verdict == "FAIL" and "UPGRADEABLE_TOKEN" in {f["code"] for f in report.findings}


async def test_buyable_but_not_sellable_fails_and_a_heavy_round_trip_fails():
    node, lp = four_node()
    node.calls.pop((lp.helper.lower(), "0x" + selector("trySell(address,uint256)").hex()))
    report, rt = await safety.check(lp, TOKEN, 10 ** 16, S)
    assert report.verdict == "FAIL" and report.sellable is False
    assert "NOT_SELLABLE" in {f["code"] for f in report.findings}

    node, lp = four_node(sell_funds=5 * 10 ** 15)  # 50% round-trip loss
    report, _ = await safety.check(lp, TOKEN, 10 ** 16, S)
    assert report.verdict == "FAIL" and "ROUND_TRIP_LOSS_TOO_HIGH" in {f["code"] for f in report.findings}


async def test_rpc_outage_is_unknown_never_safe():
    def down(req):
        return httpx.Response(503)

    lp = FourMeme(EvmRpc("bsc", 56, ["https://d.example"], client=httpx.AsyncClient(transport=httpx.MockTransport(down))))
    report, _ = await safety.check(lp, TOKEN, 10 ** 16, S)
    assert report.verdict == "UNKNOWN" and report.sellable is None


async def test_honeypot_is_is_enrichment_only():
    node, lp = four_node()
    s = evm_settings.parse({"honeypot_is_enabled": True})[0]

    def flagged(req):
        return httpx.Response(200, json={"honeypotResult": {"isHoneypot": True}, "simulationResult": {}})

    def clean(req):
        return httpx.Response(200, json={"honeypotResult": {"isHoneypot": False}, "simulationResult": {}})

    r, _ = await safety.check(lp, TOKEN, 10 ** 16, s, http=httpx.AsyncClient(transport=httpx.MockTransport(flagged)))
    assert r.verdict == "FAIL"
    node.calls.pop((lp.helper.lower(), "0x" + selector("trySell(address,uint256)").hex()))
    r, _ = await safety.check(lp, TOKEN, 10 ** 16, s, http=httpx.AsyncClient(transport=httpx.MockTransport(clean)))
    assert r.verdict == "FAIL"  # a clean Honeypot.is result never overrides a failed sell


def test_settings_validate_and_keep_chain_amounts_separate():
    s, errors = evm_settings.parse({"bsc": {"position_size": "0.05", "max_total_exposure": "0.2"},
                                    "entry_categories": ["fresh", "momentum"]})
    assert not errors and s.bsc.position_size == Decimal("0.05") and s.robinhood.position_size == Decimal("0.005")
    assert s.entry_categories == ("FRESH", "MOMENTUM")
    _, errors = evm_settings.parse({"bsc": {"position_size": "1", "max_total_exposure": "0.5"},
                                    "entry_categories": ["WHALE"], "allow_safety_warn": "yes"})
    assert len(errors) == 3


def _trade(at, buy, trader, q, t=10 ** 21):
    return SimpleNamespace(at=at, is_buy=buy, trader=trader, quote_amount=Decimal(q), token_amount=Decimal(t))


def test_trade_stats_and_categories():
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    trades = [_trade(now - timedelta(seconds=250 - i * 20), i % 5 != 4, f"0x{i % 7:040x}", 10 ** 16 + i * 10 ** 14)
              for i in range(12)]
    st = trade_stats(trades, now, 300)
    assert st["buys"] == 10 and st["sells"] == 2 and st["unique_buyers"] >= 6
    assert Decimal(st["net_buy_ratio"]) > Decimal("0.7")
    row = SimpleNamespace(migrated_at=None, created_at=now - timedelta(minutes=5), extra={"launch_seen": True})
    assert categorize(row, st, now, S) == TokenCategory.FRESH
    row.created_at = now - timedelta(hours=3)
    assert categorize(row, st, now, S) == TokenCategory.MOMENTUM
    row.extra = {"launch_seen": False}
    row.created_at = now - timedelta(minutes=1)  # never FRESH without an observed launch
    assert categorize(row, st, now, S) == TokenCategory.MOMENTUM
    row.migrated_at = now - timedelta(minutes=10)
    assert categorize(row, st, now, S) == TokenCategory.MIGRATED
    assert categorize(SimpleNamespace(migrated_at=None, created_at=now - timedelta(hours=5), extra={}),
                      trade_stats([], now, 300), now, S) == TokenCategory.OTHER
