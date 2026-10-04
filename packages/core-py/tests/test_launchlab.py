"""Raydium LaunchLab read path (master §7): decoding and trade quotes
compared with the official SDK (fixtures/launchlab_sdk/generate.cjs)."""

import base64
import json
from pathlib import Path

import pytest

from yonixalpha_core.solana import launchlab as ll

FIX = json.loads((Path(__file__).parent / "fixtures" / "launchlab_sdk" / "fixtures.json").read_text())
SQRT_OPS = {"buyExactIn", "sellExactOut"}  # linear curve: the SDK rounds a decimal.js square root


def _decode(case):
    return (ll.decode_account("PoolState", base64.b64decode(case["pool"])),
            ll.decode_account("GlobalConfig", base64.b64decode(case["config"])),
            ll.decode_account("PlatformConfig", base64.b64decode(case["platform"])))


def _ours(fn, curve_type, pool, amount, rates):
    return {"buyExactIn": ll.buy_exact_in, "buyExactOut": ll.buy_exact_out, "sellExactIn": ll.sell_exact_in,
            "sellExactOut": ll.sell_exact_out}[fn](curve_type, pool, amount, rates)


def test_every_sdk_trade_is_reproduced_exactly():
    checked = {0: 0, 1: 0, 2: 0}
    errors = capped = sqrt_rounding = 0
    for case in FIX["cases"]:
        pool, config, platform = _decode(case)
        ct = config["curve_type"]
        assert ct == case["curve_type"]
        rates = ll.FeeRates(config["trade_fee_rate"], platform["fee_rate"], platform["creator_fee_rate"], int(case["share_fee_rate"]))
        for t in case["trades"]:
            amount = int(t["amount"])
            if "error" in t:
                with pytest.raises(ll.LaunchLabError, match=t["error"]):
                    _ours(t["fn"], ct, pool, amount, rates)
                errors += 1
                continue
            r = _ours(t["fn"], ct, pool, amount, rates)
            fees = {k: int(t[k]) for k in ("protocol_fee", "platform_fee", "share_fee", "creator_fee")}
            sdk_base, sdk_quote = int(t["amount_a"]), int(t["amount_b"])
            if t["fn"] == "buyExactOut":  # the SDK echoes the amount asked for; the trade reports what is left to buy
                remaining = pool["total_base_sell"] - pool["real_base"]
                capped += amount > remaining
                sdk_base = min(sdk_base, remaining)
            if ct == ll.LINEAR_PRICE and t["fn"] in SQRT_OPS:  # only the base amount comes from the square root
                assert abs(r.base_amount - sdk_base) <= 1 and (r.quote_amount, r.fees) == (sdk_quote, fees), (t, r)
                sqrt_rounding += r.base_amount != sdk_base
            else:
                assert (r.base_amount, r.quote_amount, r.fees) == (sdk_base, sdk_quote, fees), (ct, t, r)
            checked[ct] += 1
    assert min(checked.values()) >= 90 and errors >= 20 and capped >= 10
    assert sqrt_rounding < checked[ll.LINEAR_PRICE] // 2  # the SDK's rounding differs from the integer root only sometimes


def test_decoding_reads_the_fields_the_sdk_wrote():
    pool, config, platform = _decode(FIX["cases"][0])
    assert pool["status"] == ll.FUND and pool["base_decimals"] == 6 and pool["quote_decimals"] == 9
    assert pool["token_program_flag"] == 0 and len(pool["base_mint"]) >= 32 and pool["total_base_sell"] > 0
    assert config["curve_type"] == ll.CONSTANT_PRODUCT and config["index"] == 0
    assert platform["name"][:6] == list(b"site 0")
    with pytest.raises(ll.LaunchLabError, match="not a PoolState"):
        ll.decode_account("PoolState", base64.b64decode(FIX["cases"][0]["config"]))
    with pytest.raises(ll.LaunchLabError, match="too short"):
        ll.decode_account("PoolState", base64.b64decode(FIX["cases"][0]["pool"])[:100])


def test_quote_refuses_what_is_not_modelled_or_verified():
    case = next(c for c in FIX["cases"] if c["curve_type"] == ll.CONSTANT_PRODUCT)
    pool, config, platform = _decode(case)
    buy = ll.quote_buy(pool, config, platform, 10**9)
    assert buy.base_amount > 0 and buy.quote_amount == 10**9 and buy.total_fee == ll.calculate_fee(
        10**9, config["trade_fee_rate"] + platform["fee_rate"] + platform["creator_fee_rate"])
    assert ll.price_quote_per_base(ll.CONSTANT_PRODUCT, pool) > 0 and 0 <= ll.curve_progress(pool) < 1
    with pytest.raises(ll.LaunchLabError, match="Amount is zero"):
        ll.quote_buy(pool, config, platform, 0)
    for change, reason in (({"status": ll.MIGRATE}, "not on its curve"), ({"token_program_flag": 1}, "Token-2022 base"),
                           ({"token_program_flag": 2}, "Token-2022 quote")):
        with pytest.raises(ll.LaunchLabError, match=reason):
            ll.quote_buy({**pool, **change}, config, platform, 10**9)
    with pytest.raises(ll.LaunchLabError, match="linear price curve"):
        ll.quote_sell(pool, {**config, "curve_type": ll.LINEAR_PRICE}, platform, 10)
    with pytest.raises(ll.LaunchLabError, match="Insufficient liquidity"):  # more than was ever bought
        ll.quote_sell(pool, config, platform, pool["real_base"] + 1)
    with pytest.raises(ll.LaunchLabError, match="total fee rate"):
        ll.FeeRates(600_000, 500_000, 0).total


def test_the_server_check_replays_trades_from_sdk_numbers():
    """TradeEvents built from the SDK's own trades (amounts, fees, pool
    before / after) replay exactly; a wrong amount is caught."""
    from yonixalpha_core.tools.launchlab_verify import check_pool, replay

    kinds = {}
    for case in FIX["cases"]:
        pool, config, _ = _decode(case)
        ct = config["curve_type"]
        events = []
        for t in case["trades"]:
            if "error" in t or (ct == ll.LINEAR_PRICE and t["fn"] in SQRT_OPS):
                continue
            base, quote = int(t["amount_a"]), int(t["amount_b"])
            fees = {k: int(t[k]) for k in ("protocol_fee", "platform_fee", "share_fee", "creator_fee")}
            fee = sum(fees.values())
            buy = t["fn"].startswith("buy")
            if t["fn"] == "buyExactOut":
                base = min(base, pool["total_base_sell"] - pool["real_base"])
            ev = {"total_base_sell": pool["total_base_sell"], "virtual_base": pool["virtual_base"],
                  "virtual_quote": pool["virtual_quote"], "real_base_before": pool["real_base"],
                  "real_quote_before": pool["real_quote"], "trade_direction": ll.BUY if buy else ll.SELL,
                  "exact_in": t["fn"].endswith("ExactIn"), **fees}
            if buy:
                ev.update(amount_in=quote, amount_out=base, real_base_after=pool["real_base"] + base,
                          real_quote_after=pool["real_quote"] + quote - fee)
            else:
                ev.update(amount_in=base, amount_out=quote, real_base_after=pool["real_base"] - base,
                          real_quote_after=pool["real_quote"] - quote - fee)
            r = replay(ct, ev)
            assert r["curve_equal"] and r["state_equal"], (ct, t, r)
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
            wrong = replay(ct, {**ev, "amount_out": ev["amount_out"] * 2 + 1})
            assert not (wrong["curve_equal"] and wrong["state_equal"])
            events.append(ev)
        assert check_pool(pool, events) == []
        if events:
            assert check_pool({**pool, "total_base_sell": pool["total_base_sell"] + 1}, events)
    assert set(kinds) == {"buy_exact_in", "buy_fill_rest", "buy_exact_out", "sell_exact_in", "sell_exact_out"}
    assert min(kinds.values()) >= 10, kinds
