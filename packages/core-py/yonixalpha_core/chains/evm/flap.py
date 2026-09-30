"""Flap (BSC).

All events come from the Portal. Quotes use Portal.quoteExactInput (a
non-view function, simulated with eth_call exactly as the reference sniper
does); native BNB is address(0). Token state and graduation come from
getTokenV8Safe: status 1 = Tradable on the curve, 4 = DEX (graduated).
Graduation is also read from the Portal's LaunchedToDEX(token, pool, amount,
eth) event, all fields non-indexed (layout confirmed from real BSC logs on
2026-09-30: 200M tokens and ~89.29 BNB added per graduation), so a migration
is recorded when it happens instead of only when a token is polled.
Only native-BNB quote tokens are supported in this phase.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from yonixalpha_core.chains.base import Quote, TokenCategory, TokenState
from yonixalpha_core.chains.evm import dex
from yonixalpha_core.chains.evm.abi import ZERO_ADDRESS, EventSet, event
from yonixalpha_core.chains.evm.launchpad import EvmLaunchpad, ScanResult
from yonixalpha_core.chains.registry import LAUNCHPADS

E18 = Decimal(10) ** 18
STATUS = {0: "INVALID", 1: "CURVE", 2: "IN_DUEL", 3: "KILLED", 4: "DEX", 5: "STAGED"}
_TRADE = (("ts", "uint256"), ("token", "address"), ("trader", "address"), ("amount", "uint256"), ("eth", "uint256"),
          ("fee", "uint256"), ("postPrice", "uint256"))
EVENTS = EventSet(
    event("TokenCreated", ("ts", "uint256"), ("creator", "address"), ("nonce", "uint256"), ("token", "address"),
          ("name", "string"), ("symbol", "string"), ("meta", "string")),
    event("TokenQuoteSet", ("token", "address"), ("quoteToken", "address")),
    event("FlapTokenTaxSet", ("token", "address"), ("tax", "uint256")),
    event("FlapTokenAsymmetricTaxSet", ("token", "address"), ("buyTax", "uint256"), ("sellTax", "uint256")),
    event("TokenExtensionEnabled", ("token", "address"), ("extensionID", "bytes32"), ("extensionAddress", "address"),
          ("version", "uint256")),
    event("TokenBought", *_TRADE),
    event("TokenSold", *_TRADE),
    event("LaunchedToDEX", ("token", "address"), ("pool", "address"), ("amount", "uint256"), ("eth", "uint256")),
)
STATE_TYPES = "(uint8,uint256,uint256,uint256,uint8,uint256,uint256,uint256,uint256,address,bool,bytes32,uint256,uint256,address,uint256,uint8,uint8)"
STATE_KEYS = ("status", "reserve", "circulating_supply", "price", "token_version", "r", "h", "k", "dex_supply_thresh",
              "quote_token", "native_to_quote_swap", "extension_id", "buy_tax_bps", "sell_tax_bps", "pool", "progress",
              "lp_fee_profile", "dex_id")
ZERO32 = "0x" + "00" * 32


class Flap(EvmLaunchpad):
    spec = LAUNCHPADS["flap"]
    events = EVENTS
    emitter_keys = ("portal",)

    @property
    def portal(self) -> str:
        return self.spec.contracts["portal"]

    async def _handle(self, name: str, a: dict[str, Any], log: dict[str, Any], at: datetime, res: ScanResult) -> None:
        if name == "TokenCreated":
            res.launches.append(self._launch(log, at, a["token"], a["creator"], name=a["name"], symbol=a["symbol"],
                                             extra={"nonce": a["nonce"], "meta": a["meta"][:500]}))
        elif name in ("TokenBought", "TokenSold"):
            res.trades.append(self._trade(
                log, at, token=a["token"], trader=a["trader"], is_buy=name == "TokenBought", token_amount=a["amount"],
                quote_amount=a["eth"], fee=a["fee"], price=Decimal(a["postPrice"]) / E18,
                extra={"post_price_raw": str(a["postPrice"]), "event_ts": a["ts"]}))
        elif name == "LaunchedToDEX":
            res.migrations.append({"token": a["token"], "venue": "dex", "pool": a["pool"],
                                   "tokens_added": a["amount"], "quote_added": a["eth"],
                                   "tx_hash": log.get("transactionHash"), "at": at, "evidence": "LaunchedToDEX event"})
        else:
            res.other.append({"event": name, **{k: (str(v) if isinstance(v, int) else v) for k, v in a.items()},
                              "tx_hash": log.get("transactionHash")})

    async def token_v8(self, token: str) -> dict[str, Any]:
        (vals,) = await dex.call(self.rpc, self.portal, "getTokenV8Safe(address)", [STATE_TYPES], token)
        return {k: ("0x" + v.hex() if isinstance(v, bytes) else v) for k, v in zip(STATE_KEYS, vals)}

    async def get_token_state(self, token: str) -> TokenState:
        s = await self.token_v8(token)
        stage = STATUS.get(s["status"], "UNKNOWN")
        progress = (Decimal(s["circulating_supply"]) / Decimal(s["dex_supply_thresh"])) if s["dex_supply_thresh"] else None
        return TokenState(
            chain=self.spec.chain, launchpad=self.spec.key, token=token,
            category=TokenCategory.MIGRATED if stage == "DEX" else TokenCategory.FRESH,
            stage=stage, price=Decimal(s["price"]) / E18, liquidity_quote=Decimal(s["reserve"]) / E18,
            progress=min(progress, Decimal(1)) if progress is not None else None,
            pool=s["pool"] if s["pool"] != ZERO_ADDRESS else None,
            buy_tax_bps=s["buy_tax_bps"], sell_tax_bps=s["sell_tax_bps"], source="Portal.getTokenV8Safe", at=dex.now(),
            extra={"status": s["status"], "native_quote": s["quote_token"] == ZERO_ADDRESS,
                   "quote_token": s["quote_token"], "extension_id": s["extension_id"],
                   "extension_enabled": s["extension_id"] != ZERO32, "dex_id": s["dex_id"],
                   "price_raw": str(s["price"]),
                   "price_note": "Portal price / 1e18 (scale NOT VERIFIED on chain; quotes are authoritative)"})

    async def _quote(self, token_in: str, token_out: str, amount: int, token: str) -> Quote:
        src = "flap.Portal.quoteExactInput"
        try:
            s = await self.token_v8(token)
            if s["quote_token"] != ZERO_ADDRESS:
                return dex.failed(amount, src, f"quote token {s['quote_token']} is not native BNB (unsupported)")
            if s["status"] not in (1, 4):
                return dex.failed(amount, src, f"token status {STATUS.get(s['status'], s['status'])}: not tradable")
            (out,) = await dex.call(self.rpc, self.portal, "quoteExactInput((address,address,uint256))", ["uint256"],
                                    (token_in, token_out, amount), from_=dex.SIM_ACCOUNT)
        except Exception as exc:  # noqa: BLE001
            return dex.failed(amount, src, exc)
        return Quote(ok=out > 0, amount_in=amount, amount_out=out, route="flap_portal", source=src, at=dex.now(),
                     error=None if out > 0 else "zero output")

    async def quote_buy(self, token: str, quote_in: int) -> Quote:
        return await self._quote(ZERO_ADDRESS, token, quote_in, token)

    async def quote_sell(self, token: str, tokens_in: int) -> Quote:
        return await self._quote(token, ZERO_ADDRESS, tokens_in, token)

    async def detect_migration(self, token: str) -> dict[str, Any] | None:
        s = await self.token_v8(token)
        if s["status"] != 4:
            return None
        return {"token": token, "venue": f"dex:{s['dex_id']}", "pool": s["pool"], "evidence": "getTokenV8Safe.status=DEX"}
