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
    node.on(lp.spec.contracts["manager_v2"], "buyTokenAMAP(address,uint256,uint256)", "0x")  # plain buy goes through
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


async def test_goplus_flags_fail_and_a_clean_or_missing_answer_changes_nothing():
    node, lp = four_node()
    base, _ = await safety.check(lp, TOKEN, 10 ** 16, S)
    s = evm_settings.parse({"goplus_enabled": True})[0]
    seen = []

    def answer(result):
        def handler(req):
            seen.append((req.url.path, dict(req.url.params)))
            return httpx.Response(200, json={"code": 1, "message": "OK", "result": result})
        return handler

    flagged = {TOKEN.lower(): {"is_honeypot": "0", "cannot_sell_all": "1", "hidden_owner": "1", "sell_tax": "0.1"}}
    r, _ = await safety.check(lp, TOKEN, 10 ** 16, s, http=httpx.AsyncClient(transport=httpx.MockTransport(answer(flagged))))
    f = next(x for x in r.findings if x["code"] == "GOPLUS_FLAGGED")
    assert r.verdict == "FAIL" and f["flags"] == ["cannot_sell_all"] and f["risks"] == ["hidden_owner"]
    assert seen[0] == ("/api/v1/token_security/56", {"contract_addresses": TOKEN}) and "goplus" in r.sources
    clean = {TOKEN.lower(): {"is_honeypot": "0", "cannot_sell_all": "0", "cannot_buy": "0"}}
    for result in (clean, {}):  # clean, then not indexed yet: neither changes the verdict
        r, _ = await safety.check(lp, TOKEN, 10 ** 16, s, http=httpx.AsyncClient(transport=httpx.MockTransport(answer(result))))
        assert r.verdict == base.verdict
    assert any(x["code"] == "GOPLUS_UNAVAILABLE" and "not indexed" in x["detail"] for x in r.findings)
    assert (await safety.goplus(TOKEN, "robinhood"))["available"] is False  # not covered: never called


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


def test_evm_wallet_never_exposes_the_key_and_checks_it_matches():
    from eth_account import Account

    from yonixalpha_core.chains.evm import wallet

    key = "0x" + "11" * 32
    addr = Account.from_key(key).address
    s = SimpleNamespace(EVM_WALLET_ADDRESS=None, EVM_WALLET_PRIVATE_KEY=None)
    assert wallet.account(s)["status"] == "NOT_CONFIGURED"
    s = SimpleNamespace(EVM_WALLET_ADDRESS=addr.lower(), EVM_WALLET_PRIVATE_KEY=None)
    assert wallet.account(s) | {} == {"configured": True, "address": addr, "key_configured": False, "status": "WATCH_ONLY",
                                      "detail": "address only (no signing key: none is needed for paper)"}
    s = SimpleNamespace(EVM_WALLET_ADDRESS=addr, EVM_WALLET_PRIVATE_KEY=key)
    r = wallet.account(s)
    assert r["status"] == "OK" and "11" * 32 not in str(r)
    s = SimpleNamespace(EVM_WALLET_ADDRESS="0x" + "22" * 20, EVM_WALLET_PRIVATE_KEY=key)
    assert wallet.account(s)["status"] == "MISMATCH"
    s = SimpleNamespace(EVM_WALLET_ADDRESS=None, EVM_WALLET_PRIVATE_KEY="not-a-key")
    r = wallet.account(s)
    assert r["status"] == "INVALID" and "not-a-key" not in str(r)


async def test_fourmeme_x_mode_is_detected_by_the_plain_buy_simulation():
    """X Mode: a plain buyTokenAMAP reverts "A" (four-meme-ai errors.md); the
    quote round trip still passes, so only the simulation catches it."""
    from yonixalpha_core.chains.evm import fourmeme

    node, lp = four_node()
    _, rt = await safety.check(lp, TOKEN, 10 ** 16 + 123, S)
    assert rt["plain_buy"]["status"] == fourmeme.PLAIN_BUY_OK
    buy = [r for r in node.requests if r["method"] == "eth_call" and r["params"][0]["data"].startswith(
        "0x" + selector("buyTokenAMAP(address,uint256,uint256)").hex())][-1]
    tx, _, override = buy["params"]
    assert tx["from"] == fourmeme.dex.SIM_ACCOUNT and tx["to"].lower() == lp.spec.contracts["manager_v2"].lower()
    assert int(tx["data"][74:138], 16) == 10 ** 16  # funds rounded down to GWEI ("GW")
    assert fourmeme.dex.SIM_ACCOUNT in override

    node.on(lp.spec.contracts["manager_v2"], "buyTokenAMAP(address,uint256,uint256)", Exception("execution reverted: A"))
    report, rt = await safety.check(lp, TOKEN, 10 ** 16, S)
    codes = {f["code"]: f for f in report.findings}
    assert report.verdict == "FAIL" and codes["FOURMEME_X_MODE"]["level"] == "FAIL"
    assert rt["sellable"] is True and rt["plain_buy"]["status"] == fourmeme.X_MODE  # the quotes alone pass

    node.on(lp.spec.contracts["manager_v2"], "buyTokenAMAP(address,uint256,uint256)", Exception("execution reverted: More BNB"))
    report, rt = await safety.check(lp, TOKEN, 10 ** 16, S)
    assert report.verdict == "WARN" and "More BNB" in {f["code"]: f for f in report.findings}["PLAIN_BUY_REVERTS"]["message"]

    node.reject_override = True  # a node without eth_call state overrides: stated, not blocking
    report, rt = await safety.check(lp, TOKEN, 10 ** 16, S)
    assert rt["plain_buy"]["status"] == fourmeme.NOT_SIMULATED and report.verdict == "PASS"
    assert "PLAIN_BUY_NOT_SIMULATED" in {f["code"] for f in report.findings}


def test_revert_reason_reads_error_string_data_and_messages():
    from eth_abi import encode

    from yonixalpha_core.chains.evm.fourmeme import revert_reason
    from yonixalpha_core.chains.evm.rpc import EvmRpcError

    data = "0x08c379a0" + encode(["string"], ["A"]).hex()
    assert revert_reason(EvmRpcError("execution reverted", 3, data)) == "A"
    assert revert_reason(EvmRpcError("execution reverted", 3, {"data": data})) == "A"
    assert revert_reason(EvmRpcError("execution reverted: GW", 3)) == "GW"
    assert revert_reason(EvmRpcError("execution reverted", 3)) == ""
    assert revert_reason(EvmRpcError("invalid params: too many arguments", -32602)) is None
