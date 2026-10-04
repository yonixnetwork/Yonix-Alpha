"""Meteora Dynamic Bonding Curve read path (master §7): decoding and swap
quotes compared with the official SDK (fixtures/dbc_sdk/generate.cjs)."""

import base64
import json
from pathlib import Path

import pytest

from yonixalpha_core.solana import dbc

FIX = json.loads((Path(__file__).parent / "fixtures" / "dbc_sdk" / "fixtures.json").read_text())


def _decode(case):
    return (dbc.decode_account("PoolConfig", base64.b64decode(case["config"])),
            dbc.decode_account("VirtualPool", base64.b64decode(case["pool"])))


def test_every_sdk_quote_is_reproduced_exactly():
    checked = errors = 0
    for case in FIX["cases"]:
        config, pool = _decode(case)
        for q in case["quotes"]:
            if "error" in q:
                with pytest.raises(dbc.DbcError, match=q["error"]):
                    dbc.swap_quote(pool, config, q["base_for_quote"], int(q["amount_in"]), int(q["current_point"]))
                errors += 1
                continue
            r = dbc.swap_quote(pool, config, q["base_for_quote"], int(q["amount_in"]), int(q["current_point"]))
            got = {"actual_input_amount": r.actual_input_amount, "output_amount": r.output_amount,
                   "next_sqrt_price": r.next_sqrt_price, "trading_fee": r.trading_fee, "protocol_fee": r.protocol_fee,
                   "referral_fee": r.referral_fee}
            assert got == {k: int(q[k]) for k in got}, (q, got)
            checked += 1
    assert checked >= 200 and errors >= 1  # both outcomes covered
    modes = {_decode(c)[0]["pool_fees"]["base_fee"]["base_fee_mode"] for c in FIX["cases"]}
    assert modes == {0, 1, 2}  # linear, exponential scheduler and rate limiter all compared


def test_decoding_reads_the_fields_the_sdk_wrote():
    config, pool = _decode(FIX["cases"][0])
    pts = [p for p in config["curve"] if p["sqrt_price"]]
    assert len(config["curve"]) == 20 and pts and config["migration_sqrt_price"] == pts[-1]["sqrt_price"]
    assert config["sqrt_start_price"] < pts[0]["sqrt_price"] and config["migration_quote_threshold"] == 85_000_000_000
    assert pool["pool_state"]["activation_point"] == 1_000_000 and len(pool["pool_state"]["base_mint"]) >= 32
    with pytest.raises(dbc.DbcError, match="not a VirtualPool"):
        dbc.decode_account("VirtualPool", base64.b64decode(FIX["cases"][0]["config"]))


def test_completed_pool_and_zero_amount_are_refused():
    config, pool = _decode(FIX["cases"][1])
    with pytest.raises(dbc.DbcError, match="Amount is zero"):
        dbc.swap_quote(pool, config, False, 0, 1_000_000)
    pool["pool_state"]["quote_reserve"] = config["migration_quote_threshold"]
    with pytest.raises(dbc.DbcError, match="completed"):
        dbc.swap_quote(pool, config, False, 10**6, 1_000_000)
    assert dbc.price_quote_per_base(dbc.ONE_Q64, 6, 9) == pytest.approx(1e-3)


def test_the_server_check_replays_consecutive_swaps_exactly():
    from yonixalpha_core.tools.dbc_verify import check_pool, replay

    case = next(c for c in FIX["cases"] if any("error" not in q for q in c["quotes"][:2]))
    config, pool = _decode(case)
    assert check_pool(pool, config, pool["pool_state"]["config"]) == []
    assert check_pool(pool, config, "OtherConfig111")[0].startswith("pool.config")
    # two buys in a row, as the chain would report them (EvtSwap)
    first = dbc.swap_quote(pool, config, False, 10**7, 1_000_000)
    pool2 = {"pool_state": {**pool["pool_state"], "sqrt_price": first.next_sqrt_price}}
    second = dbc.swap_quote(pool2, config, False, 2 * 10**7, 1_000_001)

    def ev(q, amount):
        return {"trade_direction": dbc.QUOTE_TO_BASE, "params": {"amount_in": amount},
                "swap_result": {"actual_input_amount": q.actual_input_amount, "output_amount": q.output_amount,
                                "next_sqrt_price": q.next_sqrt_price, "trading_fee": q.trading_fee,
                                "protocol_fee": q.protocol_fee, "referral_fee": q.referral_fee}}
    r = replay(config, ev(first, 10**7), "EvtSwap", ev(second, 2 * 10**7))
    assert r["next_sqrt_price_equal"] and r["curve_amount_equal"]
    skipped = ev(second, 2 * 10**7)
    r = replay(config, {"swap_result": {"next_sqrt_price": pool["pool_state"]["sqrt_price"]}}, "EvtSwap", skipped)
    assert not r["next_sqrt_price_equal"]  # a swap missing in between is caught, not passed


def test_every_sdk_curve_walk_is_reproduced_exactly():
    """calculate*FromAmountIn (an amount left over at the curve's end: swap2
    partial fill) and calculate*FromAmountOut (swap2 exact out), both ways."""
    seen = {}
    for case in FIX["cases"]:
        config, _ = _decode(case)
        for w in case["walks"]:
            sp, amount = int(w["sqrt_price"]), int(w["amount"])
            fn = w["fn"]
            if fn == "quote_to_base_from_amount_in":
                call = lambda: dbc.quote_to_base_from_amount_in(config, sp, amount, config["migration_sqrt_price"])  # noqa: E731,B023
            elif fn == "base_to_quote_from_amount_in":
                call = lambda: dbc.base_to_quote_from_amount_in(config, sp, amount)  # noqa: E731,B023
            elif fn == "quote_to_base_from_amount_out":
                call = lambda: (*dbc.quote_to_base_from_amount_out(config, sp, amount), 0)  # noqa: E731,B023
            else:
                call = lambda: (*dbc.base_to_quote_from_amount_out(config, sp, amount), 0)  # noqa: E731,B023
            if "error" in w:
                with pytest.raises(dbc.DbcError, match=w["error"]):
                    call()
                outcome = "error"
            else:
                assert call() == (int(w["result"]), int(w["next_sqrt_price"]), int(w["amount_left"])), w
                outcome = "left" if w["amount_left"] != "0" else "ok"
            seen[(fn, outcome)] = seen.get((fn, outcome), 0) + 1
    for fn in ("quote_to_base_from_amount_in", "base_to_quote_from_amount_in"):
        assert seen.get((fn, "ok"), 0) >= 20 and seen.get((fn, "left"), 0) >= 20
    for fn in ("quote_to_base_from_amount_out", "base_to_quote_from_amount_out"):
        assert seen.get((fn, "ok"), 0) >= 20 and seen.get((fn, "error"), 0) >= 20


def test_the_server_check_replays_partial_fill_and_exact_out_from_sdk_numbers():
    """swap2 events built from the SDK's own walks (no fee, so the curve
    amounts are the event amounts): a partial fill reports the input it
    consumed and an amount left; exact out reports the output asked for."""
    from yonixalpha_core.tools.dbc_verify import EXACT_OUT, PARTIAL_FILL, replay

    checked = {"partial_fill": 0, "exact_out": 0}
    for case in FIX["cases"]:
        config, _ = _decode(case)
        for w in case["walks"]:
            if "error" in w:
                continue
            base_for_quote = w["base_for_quote"]
            direction = dbc.BASE_TO_QUOTE if base_for_quote else dbc.QUOTE_TO_BASE
            prev = {"swap_result": {"next_sqrt_price": int(w["sqrt_price"])}}
            nofee = {"trading_fee": 0, "protocol_fee": 0, "referral_fee": 0, "next_sqrt_price": int(w["next_sqrt_price"])}
            if w["fn"].endswith("_in") and w["amount_left"] != "0":
                consumed = int(w["amount"]) - int(w["amount_left"])
                res = {**nofee, "included_fee_input_amount": consumed, "excluded_fee_input_amount": consumed,
                       "amount_left": int(w["amount_left"]), "output_amount": int(w["result"])}
                mode = PARTIAL_FILL
            elif w["fn"].endswith("_out"):
                res = {**nofee, "included_fee_input_amount": int(w["result"]), "excluded_fee_input_amount": int(w["result"]),
                       "amount_left": 0, "output_amount": int(w["amount"])}
                mode = EXACT_OUT
            else:
                continue
            ev = {"trade_direction": direction, "swap_parameters": {"swap_mode": mode}, "swap_result": res}
            r = replay(config, prev, "EvtSwap2", ev)
            assert r["next_sqrt_price_equal"] and r["curve_amount_equal"], (w, r)
            checked[r["mode"]] += 1
            if mode == EXACT_OUT:  # a different output is caught (one unit can round to the same price)
                bad = {**ev, "swap_result": {**res, "output_amount": res["output_amount"] * 2}}
                try:
                    r = replay(config, prev, "EvtSwap2", bad)
                except dbc.DbcError:
                    continue
                assert not (r["next_sqrt_price_equal"] and r["curve_amount_equal"]), w
    assert checked["partial_fill"] >= 40 and checked["exact_out"] >= 40
