"""The on-server venue checks (tools.dbc_verify, tools.launchlab_verify) run
end to end against a fake RPC that serves SDK-built pools and transactions
carrying their events: PASS when the chain agrees, FAIL when it does not."""

import asyncio
import base64
import json
from pathlib import Path

from tests.test_anchor_codec import encode
from yonixalpha_core.solana import anchor_codec, dbc, dbc_layout, launchlab as ll, launchlab_layout
from yonixalpha_core.solana.codec import b58encode
from yonixalpha_core.tools import dbc_verify, launchlab_verify

FIXTURES = Path(__file__).parent / "fixtures"
POOL = "Poo1111111111111111111111111111111111111111"


class FakeRpc:
    def __init__(self, txs, accounts):
        self.txs, self.accounts = txs, accounts

    def replace_endpoints(self, specs):
        pass

    async def call(self, method, params, priority=None):
        if method == "getSignaturesForAddress":
            return [{"signature": sig, "err": None} for sig in reversed(list(self.txs))]  # newest first
        if method == "getTransaction":
            # production 2026-10-05: these programs' transactions are version 1
            assert params[1]["maxSupportedTransactionVersion"] == 1 and params[1]["encoding"] == "jsonParsed"
            if self.txs[params[0]] == "too new":
                from yonixalpha_core.solana.rpc import RpcUnsupportedTransactionVersionError
                raise RpcUnsupportedTransactionVersionError("Transaction version (2) is not supported")
            return self.txs[params[0]]
        if method == "getMultipleAccounts":
            return {"value": [{"data": [base64.b64encode(self.accounts[a]).decode(), "base64"]} if a in self.accounts else None
                              for a in params[0]]}
        raise AssertionError(method)


def _tx(layout, events):
    """A version-1 transaction as the node renders it with jsonParsed."""
    data = [b58encode(anchor_codec.EVENT_IX_TAG + bytes(layout.EVENT_DISCRIMINATORS[name]) + encode(layout.TYPES, name, ev))
            for name, ev in events]
    return {"slot": 1, "version": 1, "transaction": {"message": {"accountKeys": [{"pubkey": layout.PROGRAM_ID}],
                                                                 "instructions": []}},
            "meta": {"innerInstructions": [{"index": 0, "instructions": [
                {"programId": layout.PROGRAM_ID, "accounts": [], "data": d, "stackHeight": 2} for d in data]}]}}


def _run(module, monkeypatch, txs, accounts):
    class Engine:
        async def dispose(self):
            pass

    class Session:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    async def effective_rpc(session, settings):
        return [{"url": "http://fake"}]

    monkeypatch.setattr(module, "get_settings", lambda: None)
    monkeypatch.setattr(module, "make_engine", lambda s: Engine())
    monkeypatch.setattr(module, "make_session_factory", lambda e: Session)
    monkeypatch.setattr(module, "effective_rpc", effective_rpc)
    monkeypatch.setattr(module.RpcManager, "create", classmethod(lambda cls, **kw: FakeRpc(txs, accounts)))
    return asyncio.run(module.main([]))


def test_launchlab_verify_passes_on_agreeing_trades_and_fails_on_a_wrong_one(monkeypatch, capsys):
    fix = json.loads((FIXTURES / "launchlab_sdk" / "fixtures.json").read_text())
    case = next(c for c in fix["cases"] if c["curve_type"] == ll.CONSTANT_PRODUCT and len(c["trades"]) == 8)
    raw = {k: base64.b64decode(case[k]) for k in ("pool", "config", "platform")}
    pool = ll.decode_account("PoolState", raw["pool"])
    accounts = {POOL: raw["pool"], pool["global_config"]: raw["config"], pool["platform_config"]: raw["platform"]}
    events = []
    for t in case["trades"]:
        if "error" in t or t["fn"] not in ("buyExactIn", "sellExactIn"):
            continue
        fees = {k: int(t[k]) for k in ("protocol_fee", "platform_fee", "creator_fee", "share_fee")}
        base, quote, buy = int(t["amount_a"]), int(t["amount_b"]), t["fn"] == "buyExactIn"
        fee = sum(fees.values())
        events.append({"pool_state": POOL, "total_base_sell": pool["total_base_sell"], "virtual_base": pool["virtual_base"],
                       "virtual_quote": pool["virtual_quote"], "real_base_before": pool["real_base"],
                       "real_quote_before": pool["real_quote"],
                       "real_base_after": pool["real_base"] + base if buy else pool["real_base"] - base,
                       "real_quote_after": pool["real_quote"] + quote - fee if buy else pool["real_quote"] - quote - fee,
                       "amount_in": quote if buy else base, "amount_out": base if buy else quote, **fees,
                       "trade_direction": ll.BUY if buy else ll.SELL, "pool_status": 0, "exact_in": True})
    assert len(events) >= 3
    txs = {f"sig{i}": _tx(launchlab_layout, [("TradeEvent", ev)]) for i, ev in enumerate(events)}
    txs["sig_new"] = "too new"  # one transaction the node cannot render: skipped and reported, never a crash
    assert _run(launchlab_verify, monkeypatch, txs, accounts) == 0
    out = capsys.readouterr().out
    assert f"exactly equal {len(events)}/{len(events)} [PASS]" in out and "RESULT: PASS" in out and "quotable now 1" in out
    assert "not readable (skipped): {'RpcUnsupportedTransactionVersionError': 1}" in out
    del txs["sig_new"]
    bad = {**events[0], "amount_out": events[0]["amount_out"] + 1}
    txs["sig_bad"] = _tx(launchlab_layout, [("TradeEvent", bad)])
    assert _run(launchlab_verify, monkeypatch, txs, accounts) == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out and "first mismatch" in out and "RESULT: FAIL" in out


def _swap_event(pool_addr, config_addr, q, amount_in):
    return {"pool": pool_addr, "config": config_addr, "trade_direction": dbc.QUOTE_TO_BASE, "has_referral": False,
            "params": {"amount_in": amount_in, "minimum_amount_out": 0},
            "swap_result": {"actual_input_amount": q.actual_input_amount, "output_amount": q.output_amount,
                            "next_sqrt_price": q.next_sqrt_price, "trading_fee": q.trading_fee,
                            "protocol_fee": q.protocol_fee, "referral_fee": q.referral_fee},
            "amount_in": amount_in, "current_timestamp": 0}


def test_dbc_verify_passes_on_consecutive_swaps_and_fails_when_one_is_off(monkeypatch, capsys):
    fix = json.loads((FIXTURES / "dbc_sdk" / "fixtures.json").read_text())
    for case in fix["cases"]:
        config = dbc.decode_account("PoolConfig", base64.b64decode(case["config"]))
        pool = dbc.decode_account("VirtualPool", base64.b64decode(case["pool"]))
        try:
            swaps, state = [], pool
            for i, amount in enumerate((10**7, 2 * 10**7, 3 * 10**7)):
                q = dbc.swap_quote(state, config, False, amount, 1_000_000 + i)
                swaps.append((q, amount))
                state = {"pool_state": {**state["pool_state"], "sqrt_price": q.next_sqrt_price}}
            break
        except dbc.DbcError:
            continue
    cfg_addr = pool["pool_state"]["config"]
    # the pool account as it is now (after the three swaps)
    pool_now = {**pool, "pool_state": {**pool["pool_state"], "sqrt_price": swaps[-1][0].next_sqrt_price}}
    pool_bytes = bytes(dbc_layout.ACCOUNT_DISCRIMINATORS["VirtualPool"]) + encode(dbc_layout.TYPES, "VirtualPool", pool_now)
    accounts = {POOL: pool_bytes, cfg_addr: base64.b64decode(case["config"])}
    txs = {f"sig{i}": _tx(dbc_layout, [("EvtSwap", _swap_event(POOL, cfg_addr, q, a))]) for i, (q, a) in enumerate(swaps)}
    assert _run(dbc_verify, monkeypatch, txs, accounts) == 0
    out = capsys.readouterr().out
    assert "layout PASS" in out and "next sqrt price equal 2/2" in out and "RESULT: PASS" in out
    q, a = swaps[2]
    off = _swap_event(POOL, cfg_addr, q, a)
    off["swap_result"]["output_amount"] += 1
    txs["sig2"] = _tx(dbc_layout, [("EvtSwap", off)])
    assert _run(dbc_verify, monkeypatch, txs, accounts) == 1
    assert "RESULT: FAIL" in capsys.readouterr().out


def _swap2_twin(ev):
    """The EvtSwap2 the program emits next to `ev` for the same swap (exact in)."""
    res = ev["swap_result"]
    return {"pool": ev["pool"], "config": ev["config"], "trade_direction": ev["trade_direction"], "has_referral": False,
            "swap_parameters": {"amount_0": ev["amount_in"], "amount_1": 0, "swap_mode": dbc_verify.EXACT_IN},
            "swap_result": {"included_fee_input_amount": ev["amount_in"], "excluded_fee_input_amount": res["actual_input_amount"],
                            "amount_left": 0, "output_amount": res["output_amount"], "next_sqrt_price": res["next_sqrt_price"],
                            "trading_fee": res["trading_fee"], "protocol_fee": res["protocol_fee"],
                            "referral_fee": res["referral_fee"]},
            "quote_reserve_amount": 0, "migration_threshold": 0, "current_timestamp": ev["current_timestamp"]}


def test_unique_swaps_keeps_one_entry_per_swap():
    a = {"pool": POOL, "trade_direction": 1, "current_timestamp": 5,
         "swap_result": {"output_amount": 10, "next_sqrt_price": 7}}
    a2 = {**a, "swap_result": {**a["swap_result"], "amount_left": 0}}
    b = {**a, "swap_result": {"output_amount": 11, "next_sqrt_price": 8}}
    assert dbc.unique_swaps([("EvtSwap", a), ("EvtSwap2", a2), ("EvtSwap", b)]) == [("EvtSwap2", a2), ("EvtSwap", b)]
    assert dbc.unique_swaps([("EvtSwap2", a2), ("EvtSwap", a)]) == [("EvtSwap2", a2)]
    # two different swaps that happen to agree are not twins unless one of each event
    assert dbc.unique_swaps([("EvtSwap", a), ("EvtSwap", a)]) == [("EvtSwap", a), ("EvtSwap", a)]
    # a different pool, direction or price is another swap
    other = {**a, "pool": "Other111111111111111111111111111111111111111"}
    assert len(dbc.unique_swaps([("EvtSwap", a), ("EvtSwap2", other)])) == 2


def test_dbc_verify_counts_a_swap_reported_by_both_events_once(monkeypatch, capsys):
    """Server run 2026-10-05: about half of the pairs were equal in every pool
    (n swaps, 2n - 1 pairs, n - 1 equal): each swap reported as EvtSwap and
    EvtSwap2 was replayed from its own twin."""
    fix = json.loads((FIXTURES / "dbc_sdk" / "fixtures.json").read_text())
    for case in fix["cases"]:
        config = dbc.decode_account("PoolConfig", base64.b64decode(case["config"]))
        pool = dbc.decode_account("VirtualPool", base64.b64decode(case["pool"]))
        try:
            swaps, state = [], pool
            for i, amount in enumerate((10**7, 2 * 10**7, 3 * 10**7)):
                q = dbc.swap_quote(state, config, False, amount, 1_000_000 + i)
                swaps.append((q, amount))
                state = {"pool_state": {**state["pool_state"], "sqrt_price": q.next_sqrt_price}}
            break
        except dbc.DbcError:
            continue
    cfg_addr = pool["pool_state"]["config"]
    pool_now = {**pool, "pool_state": {**pool["pool_state"], "sqrt_price": swaps[-1][0].next_sqrt_price}}
    pool_bytes = bytes(dbc_layout.ACCOUNT_DISCRIMINATORS["VirtualPool"]) + encode(dbc_layout.TYPES, "VirtualPool", pool_now)
    accounts = {POOL: pool_bytes, cfg_addr: base64.b64decode(case["config"])}
    txs = {}
    for i, (q, a) in enumerate(swaps):
        ev = _swap_event(POOL, cfg_addr, q, a)
        txs[f"sig{i}"] = _tx(dbc_layout, [("EvtSwap", ev), ("EvtSwap2", _swap2_twin(ev))])
    assert _run(dbc_verify, monkeypatch, txs, accounts) == 0
    out = capsys.readouterr().out
    assert "DBC swaps found in the latest program transactions: 3 in 1 pools" in out
    assert "next sqrt price equal 2/2" in out and "curve amount equal 2/2" in out and "'exact_in': 2" in out
    assert "counted once): 3" in out and "RESULT: PASS" in out
    # without counting the twins once: the server's FAIL, n - 1 of 2n - 1 equal
    monkeypatch.setattr(dbc, "unique_swaps", lambda events: events)
    assert _run(dbc_verify, monkeypatch, txs, accounts) == 1
    assert "next sqrt price equal 2/5" in capsys.readouterr().out


def test_dbc_verify_reports_an_undecodable_pool_as_fail(monkeypatch, capsys):
    fix = json.loads((FIXTURES / "dbc_sdk" / "fixtures.json").read_text())
    config = dbc.decode_account("PoolConfig", base64.b64decode(fix["cases"][0]["config"]))
    pool = dbc.decode_account("VirtualPool", base64.b64decode(fix["cases"][0]["pool"]))
    q = dbc.swap_quote(pool, config, False, 10**7, 1_000_000)
    ev = _swap_event(POOL, pool["pool_state"]["config"], q, 10**7)
    txs = {"sig0": _tx(dbc_layout, [("EvtSwap", ev)])}
    # the pool address holds a config account (wrong discriminator), its config is missing
    assert _run(dbc_verify, monkeypatch, txs, {POOL: base64.b64decode(fix["cases"][0]["config"])}) == 1
    assert "layout FAIL not a VirtualPool" in capsys.readouterr().out
