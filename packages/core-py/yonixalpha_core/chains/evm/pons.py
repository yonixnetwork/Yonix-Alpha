"""Pons (Robinhood Chain), from the official Solidity source
(github.com/ponsdotdev/pons-labs).

V2: each launch has its own bonding-curve contract, announced by the
factory's TokenLaunched(token, curve, deployer, ...). CurveBuy / CurveSell
are emitted by the curve and carry no token address, so only curves the
factory announced (or that the factory confirms through getLaunchedToken)
are accepted. Graduation (PoolGraduated) moves the token to a Uniswap V4
pool with the Pons hook; V4 trading is NOT implemented, so a graduated V2
token is observe-only.

Buy quotes simulate curve.buy with eth_call (a state override funds the
simulation account), so any tax the deployed curve applies is included;
when the node does not support state overrides the curve formula from the
source is applied locally and the quote is marked exact=False (the factory
also snapshots an anti-snipe tax for the first seconds of a launch, which
the local formula may not include). Sell quotes use the source's formula on
the live reserves (exact=False) because a sell simulation needs a token
balance and allowance the simulation account does not have.

V1 (and NOXA, same TokenLaunched layout): tokens list straight into a
locked one-sided Uniswap V3 position; trading and quotes go through V3.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from yonixalpha_core.chains.base import LaunchpadSpec, Quote, TokenCategory, TokenState
from yonixalpha_core.chains.evm import dex
from yonixalpha_core.chains.evm.abi import ZERO_ADDRESS, EventSet, event
from yonixalpha_core.chains.evm.launchpad import EvmLaunchpad, ScanResult
from yonixalpha_core.chains.evm.rpc import EvmRpcError
from yonixalpha_core.chains.evm.v3pools import V3PoolsMixin
from yonixalpha_core.chains.registry import LAUNCHPADS, ROBINHOOD_WETH

BPS = 10_000
_CURVE_TRADE_BUY = (("buyer", "address", True), ("recipient", "address", True), ("quoteIn", "uint256"),
                    ("tokensOut", "uint256"), ("fee", "uint256"), ("tax", "uint256"))
_CURVE_TRADE_SELL = (("seller", "address", True), ("recipient", "address", True), ("tokensIn", "uint256"),
                     ("quoteOut", "uint256"), ("fee", "uint256"), ("tax", "uint256"))
V2_EVENTS = EventSet(
    event("TokenLaunched", ("token", "address", True), ("curve", "address", True), ("deployer", "address", True),
          ("pairToken", "address"), ("launchConfigId", "uint256"), ("graduationThreshold", "uint256")),
    event("PoolGraduated", ("token", "address", True), ("positionId", "uint256"), ("tokenAmount", "uint256"),
          ("pairTokenAmount", "uint256")),
    event("LaunchSwept", ("token", "address", True), ("quoteOut", "uint256"), ("tokenOut", "uint256")),
    event("CurveBuy", *_CURVE_TRADE_BUY),
    event("CurveSell", *_CURVE_TRADE_SELL),
    event("CurveCompleted", ("recipient", "address"), ("quoteOut", "uint256"), ("tokenOut", "uint256")),
)
LAUNCHED_TOKEN_TYPES = "(address,address,address,address,address,uint256,uint24,int24,uint16,bool,uint8,uint256,uint256,uint256,bool)"
LAUNCHED_TOKEN_KEYS = ("token", "curve", "deployer", "creator_fee_recipient", "pair_token", "graduation_threshold",
                       "pool_fee", "tick_spacing", "creator_tax_bps", "buyback_enabled", "phase", "swept_quote",
                       "swept_tokens", "swept_at", "exists")
PHASES = {0: "NOT_GRADUATED", 1: "SWEPT", 2: "POOL_CREATED", 3: "RESCUED"}


def curve_amount_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int = 0) -> int:
    """PonsV2BondingCurveMath._amountOut."""
    with_fee = amount_in * (BPS - fee_bps)
    return with_fee * reserve_out // (reserve_in * BPS + with_fee)


class PonsV2(EvmLaunchpad):
    spec = LAUNCHPADS["pons_v2"]
    events = V2_EVENTS

    def __init__(self, rpc) -> None:
        super().__init__(rpc)
        self.curves: dict[str, str] = {}  # curve (lower) -> token
        self.curve_quote: dict[str, str] = {}  # curve (lower) -> pair token, when known
        self.rejected_curves: set[str] = set()

    @property
    def factory(self) -> str:
        return self.spec.contracts["factory"]

    def register_curve(self, curve: str, token: str, quote: str | None = None) -> None:
        self.curves[curve.lower()] = token
        if quote:
            self.curve_quote[curve.lower()] = quote

    def _is_factory(self, emitter: str) -> bool:
        return emitter == self.factory.lower()

    async def _emitters(self) -> list[str] | None:
        return [self.factory, *(c for c in self.curves)]

    async def launched(self, token: str) -> dict[str, Any]:
        (vals,) = await dex.call(self.rpc, self.factory, "getLaunchedToken(address)", [LAUNCHED_TOKEN_TYPES], token)
        return dict(zip(LAUNCHED_TOKEN_KEYS, vals))

    async def _handle(self, name: str, a: dict[str, Any], log: dict[str, Any], at: datetime, res: ScanResult) -> None:
        emitter = (log.get("address") or "").lower()
        from_factory = self._is_factory(emitter)
        if name in ("TokenLaunched", "PoolGraduated", "LaunchSwept") and not from_factory:
            res.rejected_foreign += 1
            return
        if name == "TokenLaunched":
            self.register_curve(a["curve"], a["token"], a["pairToken"])
            res.launches.append(self._launch(log, at, a["token"], a["deployer"], quote_token=a["pairToken"],
                                             extra={"curve": a["curve"], "launch_config_id": a["launchConfigId"],
                                                    "graduation_threshold": str(a["graduationThreshold"]),
                                                    "native_quote": a["pairToken"] == ZERO_ADDRESS}))
            return
        if name == "PoolGraduated":
            res.migrations.append({"token": a["token"], "venue": "uniswap_v4", "position_id": a["positionId"],
                                   "tokens": str(a["tokenAmount"]), "quote": str(a["pairTokenAmount"]),
                                   "tx_hash": log.get("transactionHash"), "at": at,
                                   "note": "Uniswap V4 trading is not implemented: observe only after graduation"})
            return
        if name == "LaunchSwept":
            res.other.append({"event": name, "token": a["token"], "tx_hash": log.get("transactionHash")})
            return
        token = self.curves.get(emitter)
        if token is None:
            res.rejected_foreign += 1
            return
        if name == "CurveCompleted":
            res.other.append({"event": name, "token": token, "tx_hash": log.get("transactionHash")})
            return
        is_buy = name == "CurveBuy"
        # CurveBuy / CurveSell name msg.sender (the official PonsV2BondingCurve
        # emits CurveBuy(msg.sender, recipient, ...)): through a router that is
        # the router. The recipient gets the tokens (buy) or the quote (sell), so
        # it is the trader whenever it differs from the caller (server, 2026-10-02:
        # 33% of curve buys came through routers). A router sell that pays the
        # router stays credited to the router: the wallet behind it is not in the
        # event (tools.trader_attribution).
        caller, recipient = (a["buyer"] if is_buy else a["seller"]), a["recipient"]
        trader = recipient if recipient and int(recipient, 16) and recipient.lower() != caller.lower() else caller
        res.trades.append(self._trade(
            log, at, token=token, trader=trader, is_buy=is_buy,
            token_amount=a["tokensOut"] if is_buy else a["tokensIn"],
            quote_amount=a["quoteIn"] if is_buy else a["quoteOut"], fee=a["fee"] + a["tax"],
            price=None, extra={"recipient": recipient, "caller": caller, "fee": a["fee"], "creator_tax": a["tax"],
                               "curve": emitter, **({"native_quote": self.curve_quote[emitter] == ZERO_ADDRESS}
                                                    if emitter in self.curve_quote else {})}))

    async def _curve_state(self, token: str) -> dict[str, Any]:
        lt = await self.launched(token)
        if not lt["exists"]:
            raise LookupError("not a Pons V2 launch")
        curve = lt["curve"]
        self.register_curve(curve, token, lt["pair_token"])
        q_res, t_res = await dex.call(self.rpc, curve, "getReserves()", ["uint256", "uint256"])
        fee_bps = (await dex.call(self.rpc, curve, "feeBps()", ["uint256"]))[0]
        tax_bps = (await dex.call(self.rpc, curve, "creatorTaxBps()", ["uint256"]))[0]
        sellable = (await dex.call(self.rpc, curve, "sellableTokens()", ["uint256"]))[0]
        graduated = (await dex.call(self.rpc, curve, "graduated()", ["bool"]))[0]
        ready = (await dex.call(self.rpc, curve, "readyToGraduate()", ["bool"]))[0]
        real_q = (await dex.call(self.rpc, curve, "realQuoteReserve()", ["uint256"]))[0]
        return {**lt, "quote_reserve": q_res, "token_reserve": t_res, "fee_bps": fee_bps, "tax_bps": tax_bps,
                "sellable": sellable, "graduated": graduated, "ready_to_graduate": ready, "real_quote": real_q}

    async def get_token_state(self, token: str) -> TokenState:
        c = await self._curve_state(token)
        done = c["graduated"] or c["phase"] != 0
        return TokenState(
            chain=self.spec.chain, launchpad=self.spec.key, token=token,
            category=TokenCategory.MIGRATED if done else TokenCategory.FRESH,
            stage="DEX" if done else ("GRADUATING" if c["ready_to_graduate"] else "CURVE"),
            price=(Decimal(c["quote_reserve"]) / Decimal(c["token_reserve"])) if c["token_reserve"] else None,
            liquidity_quote=Decimal(c["real_quote"]) / 10 ** 18,
            progress=min(Decimal(1), Decimal(c["real_quote"]) / Decimal(c["graduation_threshold"]))
            if c["graduation_threshold"] else None,
            pool=None, buy_tax_bps=c["fee_bps"] + c["tax_bps"], sell_tax_bps=c["fee_bps"] + c["tax_bps"],
            source="PonsV2 factory.getLaunchedToken + curve reserves", at=dex.now(),
            extra={"curve": c["curve"], "phase": PHASES.get(c["phase"], c["phase"]), "pair_token": c["pair_token"],
                   "native_quote": c["pair_token"] == ZERO_ADDRESS, "fee_bps": c["fee_bps"],
                   "creator_tax_bps": c["tax_bps"], "tax_note": "fee + creator tax on the quote leg; an anti-snipe "
                   "tax may apply in the first seconds (see quote source)"})

    async def quote_buy(self, token: str, quote_in: int) -> Quote:
        src = "pons_v2.curve.buy (eth_call simulation)"
        try:
            c = await self._curve_state(token)
        except Exception as exc:  # noqa: BLE001
            return dex.failed(quote_in, src, exc)
        if c["pair_token"] != ZERO_ADDRESS:
            return dex.failed(quote_in, src, f"pair token {c['pair_token']} is an ERC-20; only native ETH is supported")
        if c["graduated"] or c["phase"] != 0 or c["sellable"] == 0:
            return dex.failed(quote_in, src, "graduated to Uniswap V4: trading not implemented (observe only)")
        try:
            (out,) = await dex.call(
                self.rpc, c["curve"], "buy(uint256,uint256,address)", ["uint256"], quote_in, 0, dex.SIM_ACCOUNT,
                from_=dex.SIM_ACCOUNT, value=quote_in,
                state_override={dex.SIM_ACCOUNT: {"balance": hex(quote_in + 10 ** 18)}})
            return Quote(ok=out > 0, amount_in=quote_in, amount_out=out, route="pons_v2_curve", source=src,
                         at=dex.now(), error=None if out > 0 else "zero output")
        except EvmRpcError as exc:
            if "override" not in str(exc).lower() and "params" not in str(exc).lower():
                return dex.failed(quote_in, src, exc)  # a real revert of the buy
        except Exception as exc:  # noqa: BLE001
            return dex.failed(quote_in, src, exc)
        # The node rejected the state override: apply the source's formula.
        fee = quote_in * c["fee_bps"] // BPS
        tax = quote_in * c["tax_bps"] // BPS
        out = min(curve_amount_out(quote_in - fee - tax, c["quote_reserve"], c["token_reserve"]), c["sellable"])
        return Quote(ok=out > 0, amount_in=quote_in, amount_out=out, fee=fee + tax, route="pons_v2_curve",
                     source="pons_v2 local curve formula (node has no eth_call state override; may exclude an "
                            "anti-snipe tax)", at=dex.now(), exact=False, error=None if out > 0 else "zero output")

    async def quote_sell(self, token: str, tokens_in: int) -> Quote:
        src = "pons_v2 curve formula on live reserves"
        try:
            c = await self._curve_state(token)
        except Exception as exc:  # noqa: BLE001
            return dex.failed(tokens_in, src, exc)
        if c["pair_token"] != ZERO_ADDRESS:
            return dex.failed(tokens_in, src, f"pair token {c['pair_token']} is an ERC-20; only native ETH is supported")
        if c["graduated"] or c["phase"] != 0 or c["ready_to_graduate"]:
            return dex.failed(tokens_in, src, "curve closed for sells (graduating / graduated to Uniswap V4)")
        gross = curve_amount_out(tokens_in, c["token_reserve"], c["quote_reserve"])
        fee = gross * c["fee_bps"] // BPS
        tax = gross * c["tax_bps"] // BPS
        out = gross - fee - tax
        return Quote(ok=out > 0, amount_in=tokens_in, amount_out=out, fee=fee + tax, route="pons_v2_curve",
                     source=src, at=dex.now(), exact=False, error=None if out > 0 else "zero output")

    async def detect_migration(self, token: str) -> dict[str, Any] | None:
        lt = await self.launched(token)
        if not lt["exists"] or lt["phase"] == 0:
            return None
        return {"token": token, "venue": "uniswap_v4", "phase": PHASES.get(lt["phase"], lt["phase"]),
                "evidence": "factory.getLaunchedToken.phase", "tradable": False}


V3_LAUNCH_EVENTS = EventSet(
    event("TokenLaunched", ("token", "address", True), ("deployer", "address", True), ("dexFactory", "address", True),
          ("pairToken", "address"), ("pool", "address"), ("dexId", "uint256"), ("launchConfigId", "uint256"),
          ("positionId", "uint256"), ("restrictionsEndBlock", "uint256"), ("initialBuyAmount", "uint256")),
    dex.V3_SWAP,
)


class GeniusFun(PonsV2):
    """Genius.fun (BSC), observe only. Its factories emit Pons V2's
    TokenLaunched and its curves Pons V2's CurveBuy / CurveSell, field for
    field (DefiLlama dimension-adapters helpers/genius-fun.ts, 59c6c55), so
    the Pons V2 decoder and its curve bookkeeping apply as they are, with two
    factories. Per-token reads (getLaunchedToken, reserves) are Pons's and are
    NOT VERIFIED on Genius; nothing here quotes or trades: the venue only
    feeds launches, trades and activity."""

    spec = LAUNCHPADS["genius_fun"]

    @property
    def factories(self) -> list[str]:
        return [a for k, a in self.spec.contracts.items() if k.startswith("factory")]

    @property
    def factory(self) -> str:
        return self.factories[-1]

    def _is_factory(self, emitter: str) -> bool:
        return emitter in {f.lower() for f in self.factories}

    async def _emitters(self) -> list[str] | None:
        return [*self.factories, *(c for c in self.curves)]


class V3LaunchFactory(V3PoolsMixin, EvmLaunchpad):
    """Pons V1 and NOXA: TokenLaunched carries the Uniswap V3 pool; launch
    restrictions (same-block, max wallet, buy caps) apply until
    restrictionsEndBlock."""

    events = V3_LAUNCH_EVENTS
    factory_key = "factory"

    def __init__(self, rpc, spec: LaunchpadSpec) -> None:
        super().__init__(rpc)
        self.spec = spec
        self._pools_init()
        self.restrictions_end: dict[str, int] = {}

    @property
    def factory(self) -> str:
        return self.spec.contracts[self.factory_key]

    async def _emitters(self) -> list[str] | None:
        return [self.factory, *(m["pool"] for m in self.pools.values())]

    async def _handle(self, name: str, a: dict[str, Any], log: dict[str, Any], at: datetime, res: ScanResult) -> None:
        if name == "Swap":
            await self._handle_swap(a, log, at, res)
            return
        if (log.get("address") or "").lower() != self.factory.lower():
            res.rejected_foreign += 1
            return
        self.register_pool(a["pool"], a["token"], a["pairToken"])
        self.restrictions_end[a["token"].lower()] = a["restrictionsEndBlock"]
        res.launches.append(self._launch(log, at, a["token"], a["deployer"], quote_token=a["pairToken"], extra={
            "pool": a["pool"], "dex_factory": a["dexFactory"], "dex_id": a["dexId"], "position_id": a["positionId"],
            "restrictions_end_block": a["restrictionsEndBlock"], "initial_buy": str(a["initialBuyAmount"]),
            "native_quote": a["pairToken"].lower() == ROBINHOOD_WETH.lower()}))

    async def get_token_state(self, token: str) -> TokenState:
        st = await self.v3_state(token, TokenCategory.FRESH)
        st.extra["restrictions_end_block"] = self.restrictions_end.get(token.lower())
        return st

    async def quote_buy(self, token: str, quote_in: int) -> Quote:
        return await self.v3_quote(token, quote_in, buy=True)

    async def quote_sell(self, token: str, tokens_in: int) -> Quote:
        return await self.v3_quote(token, tokens_in, buy=False)


def pons_v1(rpc) -> V3LaunchFactory:
    return V3LaunchFactory(rpc, LAUNCHPADS["pons_v1"])


def noxa(rpc) -> V3LaunchFactory:
    ad = V3LaunchFactory(rpc, LAUNCHPADS["noxa"])
    ad.factory_key = "launch_factory"
    return ad
