"""Price, USD market cap and liquidity of an EVM token (master §54, §61).

market cap = price x total supply (= FDV, as for Pump.fun tokens), in USD (never primarily in BNB /
ETH). Price is the launchpad's own price (native coin per whole token,
TokenState.price); supply and decimals are read from the token contract
once (data-evm, safety pass). Each value that cannot be computed is None
with the reason:
  - no fresh BNB/USD or ETH/USD rate (chains.evm.native_price);
  - supply / decimals not read yet;
  - a curve quoted in another token (Four.meme tokenized stocks): its
    price is in that token, not in BNB, so no BNB or USD figure is given.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from yonixalpha_core.chains.evm.store import NATIVE_QUOTES


def _dec(v: Any) -> Decimal | None:
    try:
        return Decimal(str(v)) if v is not None and v != "" else None
    except InvalidOperation:
        return None


def _s(v: Decimal | None) -> str | None:
    """Plain notation, never exponent form (prices are tiny: 0.00000002)."""
    return None if v is None else f"{v:f}"


def market(chain: str, state: dict[str, Any] | None, extra: dict[str, Any] | None, quote_token: str | None,
           native_usd: str | None) -> dict[str, Any]:
    state, extra = state or {}, extra or {}
    currency = {"bsc": "BNB", "robinhood": "ETH"}.get(chain, "")
    reasons: list[str] = []
    if quote_token and quote_token.lower() not in NATIVE_QUOTES:
        return {"currency": None, "price_native": None, "price_usd": None, "market_cap_usd": None,
                "liquidity_native": None, "liquidity_usd": None,
                "reasons": [f"quoted in {quote_token}, not {currency}: no {currency} or USD figure is computed"]}
    price = _dec(state.get("price"))
    liq = _dec(state.get("liquidity_quote"))
    usd = _dec(native_usd)
    supply_raw, decimals = _dec(extra.get("total_supply")), extra.get("decimals")
    supply = supply_raw / (Decimal(10) ** int(decimals)) if supply_raw is not None and decimals is not None else None
    if price is None:
        reasons.append("no price read yet")
    if usd is None:
        reasons.append(f"no fresh {currency}/USD rate")
    if supply is None:
        reasons.append("total supply / decimals not read yet")
    mcap = price * supply * usd if price is not None and supply is not None and usd is not None else None
    return {"currency": currency, "price_native": _s(price),
            "price_usd": _s(price * usd) if price is not None and usd is not None else None,
            "market_cap_usd": _s(mcap.quantize(Decimal("0.01"))) if mcap is not None else None,
            "total_supply": _s(supply), "liquidity_native": _s(liq),
            "liquidity_usd": _s((liq * usd).quantize(Decimal("0.01"))) if liq is not None and usd is not None else None,
            "reasons": reasons}
