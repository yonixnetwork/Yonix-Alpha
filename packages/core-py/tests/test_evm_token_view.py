"""EVM market cap in USD (master §61): price x supply x native USD; None with
the reason when any part is missing, and no BNB / USD figure for a curve
quoted in another token."""

from yonixalpha_core.chains.evm.token_view import market

STATE = {"price": "0.00000002", "liquidity_quote": "3.5"}
EXTRA = {"total_supply": str(10 ** 27), "decimals": 18}


def test_market_cap_in_usd():
    m = market("bsc", STATE, EXTRA, "0x0000000000000000000000000000000000000000", "600")
    assert m["market_cap_usd"] == "12000.00" and m["liquidity_usd"] == "2100.00" and m["currency"] == "BNB"
    assert m["total_supply"] == "1000000000" and m["reasons"] == []


def test_missing_parts_are_reasons_never_zero():
    m = market("robinhood", STATE, {}, None, None)
    assert m["market_cap_usd"] is None and m["price_usd"] is None and m["price_native"] == "0.00000002"
    assert set(m["reasons"]) == {"no fresh ETH/USD rate", "total supply / decimals not read yet"}
    stock = market("bsc", STATE, EXTRA, "0x4902c5ebc598265ed2212b559b042de8a5eeec3f", "600")
    assert stock["market_cap_usd"] is None and stock["price_native"] is None and "quoted in" in stock["reasons"][0]
