"""Four.meme (BSC).

Events come from TokenManager2 (V2); V1 tokens (created before 2024-09-05)
emit on the V1 manager with another layout and are traded, not discovered.
Quotes use TokenManagerHelper3.tryBuy / trySell (the launchpad's own
pre-calculation for both versions); after liquidity is added the token
trades on PancakeSwap and is quoted through the PancakeSwap V2 router.
Tokens whose quote asset is a BEP-20 (not BNB) are reported, not quoted.

X Mode (four-meme-ai errors.md): an X Mode token can only be bought with
the signed `buyToken(bytes,uint256,bytes)`; a plain buyTokenAMAP reverts
with "A". `plain_buy` simulates that plain buy with eth_call (a state
override funds the simulation account), so X Mode is detected from the
contract's own answer, without decoding the undocumented `_tokenInfos`
struct. YonixAlpha has no X Mode buy, so such a token cannot be entered.

Quote asset per token. Since 2026-09-14 most new Four.meme curves are
quoted in BEP-20 tokens, mainly tokenized stocks (bStocks: BNCB, NVDAB,
SPCXB ...; DefiLlama issue #9736; on the server 32 of the 40 newest curve
tokens, 2026-10-02). TokenPurchase / TokenSale carry no quote address and
their `cost` is in the quote token's units, so every token's quote is read
once (Helper3 getTokenInfo, cached) and each launch / trade records whether
it is native BNB (`native_quote`). BNB volume, wallet profit and loss and
market regimes leave the other quotes out (store.native_quote_trade).

Units: amounts are in wei (18 decimals for BNB and Four.meme tokens);
`price` fields in events and getTokenInfo are quote-wei per 1e18 token-wei,
i.e. BNB per whole token after dividing by 1e18.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from yonixalpha_core.chains.base import Quote, TokenCategory, TokenState
from yonixalpha_core.chains.evm import dex
from yonixalpha_core.chains.evm.abi import ZERO_ADDRESS, EventSet, decode_output, encode_call, event
from yonixalpha_core.chains.evm.launchpad import EvmLaunchpad, ScanResult
from yonixalpha_core.chains.evm.rpc import EvmRpcError, EvmRpcUnavailableError
from yonixalpha_core.chains.registry import BSC_PANCAKE_V2, BSC_WBNB, LAUNCHPADS

E18 = Decimal(10) ** 18
_TRADE = (("token", "address"), ("account", "address"), ("price", "uint256"), ("amount", "uint256"),
          ("cost", "uint256"), ("fee", "uint256"), ("offers", "uint256"), ("funds", "uint256"))
EVENTS = EventSet(
    event("TokenCreate", ("creator", "address"), ("token", "address"), ("requestId", "uint256"), ("name", "string"),
          ("symbol", "string"), ("totalSupply", "uint256"), ("launchTime", "uint256"), ("launchFee", "uint256")),
    event("TokenPurchase", *_TRADE),
    event("TokenSale", *_TRADE),
    event("LiquidityAdded", ("base", "address"), ("offers", "uint256"), ("quote", "address"), ("funds", "uint256")),
)
TOKEN_INFO_TYPES = ["uint256", "address", "address", "uint256", "uint256", "uint256", "uint256", "uint256",
                    "uint256", "uint256", "uint256", "bool"]
TOKEN_INFO_KEYS = ("version", "token_manager", "quote", "last_price", "trading_fee_rate", "min_trading_fee",
                   "launch_time", "offers", "max_offers", "funds", "max_funds", "liquidity_added")
GWEI = 10 ** 9
X_MODE_REVERT = "A"
# plain_buy statuses
PLAIN_BUY_OK, X_MODE, PLAIN_BUY_REVERTS = "PLAIN_BUY_OK", "X_MODE", "PLAIN_BUY_REVERTS"
NOT_SIMULATED, UNAVAILABLE, NOT_APPLICABLE = "NOT_SIMULATED", "UNAVAILABLE", "NOT_APPLICABLE"
ERROR_STRING = "0x08c379a0"  # Error(string)


def revert_reason(exc: Exception) -> str | None:
    """The revert string of a failed eth_call, or None when the error is not a
    revert (e.g. the node refused the request)."""
    data = getattr(exc, "data", None)
    if isinstance(data, dict):
        data = data.get("data")
    if isinstance(data, str) and data.startswith(ERROR_STRING):
        try:
            return decode_output(["string"], "0x" + data[len(ERROR_STRING):])[0]
        except Exception:  # noqa: BLE001 - malformed: fall back to the message
            pass
    msg = str(exc)
    if "execution reverted" in msg.lower():
        return msg[msg.lower().index("execution reverted") + len("execution reverted"):].lstrip(": ").strip()
    return None


QUOTE_CACHE_MAX = 50_000


class FourMeme(EvmLaunchpad):
    spec = LAUNCHPADS["fourmeme"]
    events = EVENTS
    emitter_keys = ("manager_v2",)

    def __init__(self, rpc) -> None:
        super().__init__(rpc)
        self.quotes: dict[str, str | None] = {}  # token (lower) -> quote address (ZERO = BNB); None: unreadable

    async def quote_of(self, token: str) -> str | None:
        """The token's quote asset (cached). A revert (not a Four.meme token)
        is None; an RPC outage propagates, so the scan retries later."""
        k = token.lower()
        if k not in self.quotes:
            if len(self.quotes) >= QUOTE_CACHE_MAX:
                for old in list(self.quotes)[:QUOTE_CACHE_MAX // 2]:
                    del self.quotes[old]
            try:
                self.quotes[k] = (await self.token_info(token))["quote"]
            except EvmRpcError:
                self.quotes[k] = None
        return self.quotes[k]

    async def _quote_extra(self, token: str) -> tuple[str | None, dict[str, Any]]:
        q = await self.quote_of(token)
        return q, ({} if q is None else {"native_quote": q == ZERO_ADDRESS})

    @property
    def helper(self) -> str:
        return self.spec.contracts["helper3"]

    async def _handle(self, name: str, a: dict[str, Any], log: dict[str, Any], at: datetime, res: ScanResult) -> None:
        if name == "TokenCreate":
            quote, qx = await self._quote_extra(a["token"])
            res.launches.append(self._launch(log, at, a["token"], a["creator"], name=a["name"], symbol=a["symbol"],
                                             quote_token=quote,
                                             extra={"request_id": a["requestId"], "total_supply": a["totalSupply"],
                                                    "launch_time": a["launchTime"], "launch_fee": a["launchFee"], **qx}))
        elif name in ("TokenPurchase", "TokenSale"):
            _quote, qx = await self._quote_extra(a["token"])
            res.trades.append(self._trade(
                log, at, token=a["token"], trader=a["account"], is_buy=name == "TokenPurchase",
                token_amount=a["amount"], quote_amount=a["cost"], fee=a["fee"], price=Decimal(a["price"]) / E18,
                extra={"offers_left": a["offers"], "funds": a["funds"], **qx}))
        elif name == "LiquidityAdded":
            res.migrations.append({"token": a["base"], "venue": "pancakeswap", "quote": a["quote"],
                                   "tokens_added": a["offers"], "quote_added": a["funds"],
                                   "tx_hash": log.get("transactionHash"), "at": at})

    async def token_info(self, token: str) -> dict[str, Any]:
        vals = await dex.call(self.rpc, self.helper, "getTokenInfo(address)", TOKEN_INFO_TYPES, token)
        return dict(zip(TOKEN_INFO_KEYS, vals))

    async def _tax_bps(self, token: str) -> tuple[int | None, str]:
        """TaxToken (creatorType 5) exposes feeRate() in bps on the token. A
        revert means the token has no such function (not a TaxToken); an RPC
        outage propagates, so 'unknown' is never read as 'no tax'."""
        try:
            return (await dex.call(self.rpc, token, "feeRate()", ["uint256"]))[0], "TaxToken.feeRate()"
        except EvmRpcError:
            return 0, "no feeRate(): not a Four.meme TaxToken"

    async def get_token_state(self, token: str) -> TokenState:
        info = await self.token_info(token)
        tax, tax_src = await self._tax_bps(token)
        migrated = bool(info["liquidity_added"])
        return TokenState(
            chain=self.spec.chain, launchpad=self.spec.key, token=token,
            category=TokenCategory.MIGRATED if migrated else TokenCategory.FRESH,
            stage="DEX" if migrated else "CURVE", price=Decimal(info["last_price"]) / E18,
            liquidity_quote=Decimal(info["funds"]) / E18,
            progress=(Decimal(info["funds"]) / Decimal(info["max_funds"])) if info["max_funds"] else None,
            pool=None, buy_tax_bps=tax, sell_tax_bps=tax, source="TokenManagerHelper3.getTokenInfo", at=dex.now(),
            extra={**{k: (str(v) if isinstance(v, int) and not isinstance(v, bool) else v) for k, v in info.items()},
                   "native_quote": info["quote"] == ZERO_ADDRESS, "tax_source": tax_src})

    async def quote_buy(self, token: str, quote_in: int) -> Quote:
        src = "fourmeme.tryBuy"
        try:
            info = await self.token_info(token)
            if info["quote"] != ZERO_ADDRESS:
                return dex.failed(quote_in, src, f"quote asset {info['quote']} is a BEP-20; only BNB is supported")
            if info["liquidity_added"]:
                return await dex.v2_quote(self.rpc, BSC_PANCAKE_V2["router"], [BSC_WBNB, token], quote_in,
                                          "pancakeswap_v2.getAmountsOut")
            _mgr, _q, amount, cost, fee, *_ = await dex.call(
                self.rpc, self.helper, "tryBuy(address,uint256,uint256)",
                ["address", "address", "uint256", "uint256", "uint256", "uint256", "uint256", "uint256"],
                token, 0, quote_in)
        except Exception as exc:  # noqa: BLE001 - RPC / revert: an unavailable quote, never a price
            return dex.failed(quote_in, src, exc)
        return Quote(ok=amount > 0, amount_in=quote_in, amount_out=amount, fee=fee, route="fourmeme_curve",
                     source=src, at=dex.now(), error=None if amount > 0 else "zero output")

    async def quote_sell(self, token: str, tokens_in: int) -> Quote:
        src = "fourmeme.trySell"
        try:
            info = await self.token_info(token)
            if info["quote"] != ZERO_ADDRESS:
                return dex.failed(tokens_in, src, f"quote asset {info['quote']} is a BEP-20; only BNB is supported")
            if info["liquidity_added"]:
                return await dex.v2_quote(self.rpc, BSC_PANCAKE_V2["router"], [token, BSC_WBNB], tokens_in,
                                          "pancakeswap_v2.getAmountsOut")
            _mgr, _q, funds, fee = await dex.call(self.rpc, self.helper, "trySell(address,uint256)",
                                                  ["address", "address", "uint256", "uint256"], token, tokens_in)
        except Exception as exc:  # noqa: BLE001
            return dex.failed(tokens_in, src, exc)
        # Helper3 docs: `funds` is what the seller receives, `fee` the trading fee.
        return Quote(ok=funds > 0, amount_in=tokens_in, amount_out=funds, fee=fee, route="fourmeme_curve",
                     source=src, at=dex.now(), error=None if funds > 0 else "zero output")

    async def plain_buy(self, token: str, funds: int) -> dict[str, Any]:
        """Simulates TokenManager2.buyTokenAMAP(token, funds, 0) with the
        msg.value tryBuy asks for. Returns {"status", "reason", "source"}:
          PLAIN_BUY_OK       the plain buy goes through now
          X_MODE             reverted "A": X Mode, only the signed buy works
          PLAIN_BUY_REVERTS  reverted for another reason (given)
          NOT_SIMULATED      the node refused the request (e.g. no state override)
          UNAVAILABLE        no RPC endpoint answered
          NOT_APPLICABLE     V1 token, on PancakeSwap, or a BEP-20 quote"""
        src = "TokenManager2.buyTokenAMAP (eth_call simulation)"
        funds = funds - funds % GWEI  # amounts must be GWEI-aligned ("GW")
        try:
            info = await self.token_info(token)
            if info["version"] != 2 or info["liquidity_added"] or info["quote"] != ZERO_ADDRESS:
                return {"status": NOT_APPLICABLE, "reason": f"version {info['version']}, liquidity added "
                        f"{info['liquidity_added']}, quote {info['quote']}", "source": "getTokenInfo"}
            *_, msg_value, _approval, _funds = await dex.call(
                self.rpc, self.helper, "tryBuy(address,uint256,uint256)",
                ["address", "address", "uint256", "uint256", "uint256", "uint256", "uint256", "uint256"], token, 0, funds)
            await self.rpc.eth_call(
                info["token_manager"], encode_buy_amap(token, funds), from_=dex.SIM_ACCOUNT, value=msg_value,
                state_override={dex.SIM_ACCOUNT: {"balance": hex(msg_value + 10 ** 18)}})
        except EvmRpcUnavailableError as exc:
            return {"status": UNAVAILABLE, "reason": str(exc)[:200], "source": src}
        except EvmRpcError as exc:
            reason = revert_reason(exc)
            if reason is None:
                return {"status": NOT_SIMULATED, "reason": str(exc)[:200], "source": src}
            if reason == X_MODE_REVERT:
                return {"status": X_MODE, "reason": "reverted \"A\": X Mode token, a plain buy is refused", "source": src}
            return {"status": PLAIN_BUY_REVERTS, "reason": reason or "reverted without a message", "source": src}
        return {"status": PLAIN_BUY_OK, "reason": None, "source": src}

    async def detect_migration(self, token: str) -> dict[str, Any] | None:
        info = await self.token_info(token)
        if not info["liquidity_added"]:
            return None
        return {"token": token, "venue": "pancakeswap", "evidence": "getTokenInfo.liquidityAdded"}


def encode_buy_amap(token: str, funds: int, min_amount: int = 0) -> str:
    return encode_call("buyTokenAMAP(address,uint256,uint256)", token, funds, min_amount)
