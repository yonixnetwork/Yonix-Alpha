"""Third-party router contracts seen in the M9 reference code (master §12).

A trade that goes through a router reaches the launchpad from the router, so
the launchpad event names the router as buyer / seller. Pons curves emit
CurveBuy(buyer, recipient, ...): through a router the buyer is the router and
the recipient is the wallet that gets the tokens; through a router sell both
are usually the router, and only the transaction sender identifies the wallet.

These addresses come from the repositories' source code, not from Pons or
Robinhood, and are used only to label traders in reports (tools.trader_attribution).
Nothing trusts them, routes through them or treats them as safe.
"""

from __future__ import annotations

KNOWN_ROUTERS: dict[str, dict[str, dict[str, str]]] = {
    "robinhood": {
        "0xe33e9e479df8802cb0866d5d05258bec4cf62948": {
            "label": "Pons launchAndBuy router", "source": "pons-launch-engine src/protocol/contracts.ts; on-chain launches (M5)"},
        "0x0102e02037ee0ae13257f9f825777878f967e31b": {
            "label": "pons-terminal TradeRouter V1 (0.5% fee per leg)", "source": "yesiambroke/pons-terminal src/config.ts @e678f69"},
        "0xb8f70e2acf34185a8e72d7bd33e7242da1e63b06": {
            "label": "pons-terminal TradeRouter V2 / HOODL (0.5% fee per leg, roundTrip volume)",
            "source": "yesiambroke/pons-terminal src/config.ts @e678f69"},
        "0xcaf681a66d020601342297493863e78c959e5cb2": {
            "label": "Uniswap V3 SwapRouter02", "source": "registry ROBINHOOD_UNISWAP_V3"},
        "0x8876789976decbfcbbbe364623c63652db8c0904": {
            "label": "Uniswap V4 UniversalRouter", "source": "pons-launch-engine src/protocol/contracts.ts @29205ab"},
    },
    "bsc": {},
}


def router_label(chain: str, address: str | None) -> str | None:
    return (KNOWN_ROUTERS.get(chain, {}).get((address or "").lower()) or {}).get("label")
