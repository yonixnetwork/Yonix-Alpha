"""The Odyssey (Robinhood Chain): three factory variants, ABIs from the
Blockscout-verified implementations (via hood-oracle / hoodchain SDK).

- Curve (bonding + legacy factories): native-ETH curve traded on the
  factory; Traded(token, trader, isBuy, ...) is emitted by the factory.
  quoteBuy(token, tokensOut) is exact-OUT, so a buy quote for an ETH budget
  first solves the curve locally (virtual reserves, fee on top) and then
  asks the contract's quoteBuy for that output, shrinking once if the
  contract's totalIn exceeds the budget. quoteSell is exact-IN. After
  PoolCompleted + PoolMigrated the token trades on the announced Uniswap V3
  pool.
- Instant: InstantTokenCreated carries the Uniswap V3 pool; trading is V3.
- Reflection: observe only (reflection balances; graduates to Uniswap V4).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from yonixalpha_core.chains.base import Quote, TokenCategory, TokenState
from yonixalpha_core.chains.evm import dex
from yonixalpha_core.chains.evm.abi import EventSet, event, topic_address
from yonixalpha_core.chains.evm.launchpad import EvmLaunchpad, ScanResult
from yonixalpha_core.chains.evm.rpc import EvmRpcError
from yonixalpha_core.chains.evm.v3pools import V3PoolsMixin
from yonixalpha_core.chains.registry import LAUNCHPADS

BPS = 10_000
TRADED = event("Traded", ("token", "address", True), ("trader", "address", True), ("isBuy", "bool"),
               ("tokenAmount", "uint256"), ("quoteAmount", "uint256"), ("fee", "uint256"), ("virtualQuote", "uint256"),
               ("virtualToken", "uint256"))
POOL_MIGRATED = event("PoolMigrated", ("token", "address", True), ("pool", "address"), ("tokenId", "uint256"),
                      ("liquidity", "uint128"), ("tokenUsed", "uint256"), ("quoteUsed", "uint256"))
CURVE_EVENTS = EventSet(
    event("TokenCreated", ("token", "address", True), ("creator", "address", True), ("backingWallet", "address"),
          ("isMarginBacked", "bool"), ("threshold", "uint256")),
    TRADED,
    event("PoolCompleted", ("token", "address", True), ("realQuoteRaised", "uint256"), ("lpTokenReserve", "uint256")),
    POOL_MIGRATED,
    dex.V3_SWAP,
)
# getPool(token): the first eight words are the same in the bonding and legacy
# implementations (static struct, so a prefix decodes exactly).
POOL_PREFIX = ["address", "address", "bool", "bool", "uint256", "uint256", "uint256", "uint256"]
POOL_KEYS = ("creator", "backing_wallet", "is_margin_backed", "completed", "virtual_quote", "virtual_token",
             "virtual_quote_init", "real_quote")


def _trade_kwargs(a: dict[str, Any]) -> dict[str, Any]:
    vt = a["virtualToken"]
    return {"token": a["token"], "trader": a["trader"], "is_buy": a["isBuy"], "token_amount": a["tokenAmount"],
            "quote_amount": a["quoteAmount"], "fee": a["fee"],
            "price": (Decimal(a["virtualQuote"]) / Decimal(vt)) if vt else None,
            "extra": {"virtual_quote": str(a["virtualQuote"]), "virtual_token": str(vt), "venue": "odyssey_curve"}}


class OdysseyCurve(V3PoolsMixin, EvmLaunchpad):
    spec = LAUNCHPADS["odyssey_curve"]
    events = CURVE_EVENTS
    factory_keys = ("bonding_curve_factory", "legacy_factory")
    migration_lookback_blocks = 3_000_000
    migration_span = 100_000

    def __init__(self, rpc) -> None:
        super().__init__(rpc)
        self._pools_init()
        self.token_factory: dict[str, str] = {}
        self.threshold: dict[str, int] = {}

    @property
    def factories(self) -> list[str]:
        return [self.spec.contracts[k] for k in self.factory_keys]

    async def _emitters(self) -> list[str] | None:
        return [*self.factories, *(m["pool"] for m in self.pools.values())]

    async def _handle(self, name: str, a: dict[str, Any], log: dict[str, Any], at: datetime, res: ScanResult) -> None:
        if name == "Swap":
            await self._handle_swap(a, log, at, res)
            return
        emitter = (log.get("address") or "").lower()
        if emitter not in {f.lower() for f in self.factories}:
            res.rejected_foreign += 1
            return
        if name == "TokenCreated":
            self.token_factory[a["token"].lower()] = log["address"]
            self.threshold[a["token"].lower()] = a["threshold"]
            res.launches.append(self._launch(log, at, a["token"], a["creator"], extra={
                "factory": log["address"], "backing_wallet": a["backingWallet"], "margin_backed": a["isMarginBacked"],
                "threshold": str(a["threshold"])}))
        elif name == "Traded":
            res.trades.append(self._trade(log, at, **_trade_kwargs(a)))
        elif name == "PoolCompleted":
            res.other.append({"event": name, "token": a["token"], "real_quote_raised": str(a["realQuoteRaised"]),
                              "tx_hash": log.get("transactionHash")})
        elif name == "PoolMigrated":
            self.register_pool(a["pool"], a["token"])
            res.migrations.append({"token": a["token"], "venue": "uniswap_v3", "pool": a["pool"],
                                   "tokens": str(a["tokenUsed"]), "quote": str(a["quoteUsed"]),
                                   "tx_hash": log.get("transactionHash"), "at": at})

    async def _factory_for(self, token: str) -> tuple[str, dict[str, Any]]:
        known = self.token_factory.get(token.lower())
        for f in ([known] if known else []) + [f for f in self.factories if f != known]:
            try:
                vals = await dex.call(self.rpc, f, "getPool(address)", POOL_PREFIX, token)
            except EvmRpcError:
                continue
            pool = dict(zip(POOL_KEYS, vals))
            if int(pool["creator"], 16) != 0:
                self.token_factory[token.lower()] = f
                return f, pool
        raise LookupError("token is not an Odyssey curve launch")

    async def get_token_state(self, token: str) -> TokenState:
        if token.lower() in self.token_pool:
            return await self.v3_state(token, TokenCategory.MIGRATED)
        factory, p = await self._factory_for(token)
        fee_bps = (await dex.call(self.rpc, factory, "feeBps()", ["uint256"]))[0]
        thr = self.threshold.get(token.lower())
        return TokenState(
            chain=self.spec.chain, launchpad=self.spec.key, token=token,
            category=TokenCategory.MIGRATED if p["completed"] else TokenCategory.FRESH,
            stage="GRADUATING" if p["completed"] else "CURVE",
            price=(Decimal(p["virtual_quote"]) / Decimal(p["virtual_token"])) if p["virtual_token"] else None,
            liquidity_quote=Decimal(p["real_quote"]) / 10 ** 18,
            progress=min(Decimal(1), Decimal(p["real_quote"]) / Decimal(thr)) if thr else None,
            pool=None, buy_tax_bps=fee_bps, sell_tax_bps=fee_bps, source="odyssey factory.getPool", at=dex.now(),
            extra={"factory": factory, "completed": p["completed"], "fee_bps": fee_bps,
                   "threshold_known": thr is not None})

    async def quote_buy(self, token: str, quote_in: int) -> Quote:
        if token.lower() in self.token_pool:
            return await self.v3_quote(token, quote_in, buy=True)
        src = "odyssey factory.quoteBuy"
        try:
            factory, p = await self._factory_for(token)
            if p["completed"]:
                return dex.failed(quote_in, src, "curve completed; migrated pool not yet known (see detect_migration)")
            fee_bps = (await dex.call(self.rpc, factory, "feeBps()", ["uint256"]))[0]
            vq, vt = p["virtual_quote"], p["virtual_token"]
            cost = max(0, quote_in * BPS // (BPS + fee_bps) - 2)
            want = vt * cost // (vq + cost) if vq + cost else 0
            for _ in range(2):
                if want <= 0:
                    return dex.failed(quote_in, src, "budget too small for any output")
                cost_q, fee, total_in, actual_out, will_grad = await dex.call(
                    self.rpc, factory, "quoteBuy(address,uint256)", ["uint256", "uint256", "uint256", "uint256", "bool"],
                    token, want)
                if total_in <= quote_in:
                    return Quote(ok=actual_out > 0, amount_in=total_in, amount_out=actual_out, fee=fee,
                                 route="odyssey_curve", source=src + (" (graduates on this buy)" if will_grad else ""),
                                 at=dex.now(), error=None if actual_out > 0 else "zero output")
                want = want * quote_in // total_in - 1
            return dex.failed(quote_in, src, "contract quote exceeds the budget")
        except Exception as exc:  # noqa: BLE001
            return dex.failed(quote_in, src, exc)

    async def quote_sell(self, token: str, tokens_in: int) -> Quote:
        if token.lower() in self.token_pool:
            return await self.v3_quote(token, tokens_in, buy=False)
        src = "odyssey factory.quoteSell"
        try:
            factory, p = await self._factory_for(token)
            if p["completed"]:
                return dex.failed(tokens_in, src, "curve completed; migrated pool not yet known")
            _gross, fee, user_gets = await dex.call(self.rpc, factory, "quoteSell(address,uint256)",
                                                    ["uint256", "uint256", "uint256"], token, tokens_in)
        except Exception as exc:  # noqa: BLE001
            return dex.failed(tokens_in, src, exc)
        return Quote(ok=user_gets > 0, amount_in=tokens_in, amount_out=user_gets, fee=fee, route="odyssey_curve",
                     source=src, at=dex.now(), error=None if user_gets > 0 else "zero output")

    async def detect_migration(self, token: str) -> dict[str, Any] | None:
        pool = self.token_pool.get(token.lower())
        if pool:
            return {"token": token, "venue": "uniswap_v3", "pool": pool, "evidence": "PoolMigrated event"}
        _f, p = await self._factory_for(token)
        if not p["completed"]:
            return None
        head = await self.rpc.block_number()
        logs = await self.rpc.get_logs(self.factories, [POOL_MIGRATED.topic, topic_address(token)],
                                       max(0, head - self.migration_lookback_blocks), head, max_span=self.migration_span)
        for log in logs:
            a = POOL_MIGRATED.decode(log)
            self.register_pool(a["pool"], token)
            return {"token": token, "venue": "uniswap_v3", "pool": a["pool"], "evidence": "PoolMigrated event",
                    "tx_hash": log.get("transactionHash")}
        return {"token": token, "venue": "uniswap_v3", "pool": None,
                "evidence": "getPool.completed; PoolMigrated not found in the lookback window"}


INSTANT_EVENTS = EventSet(
    event("InstantTokenCreated", ("token", "address", True), ("creator", "address", True), ("backingWallet", "address"),
          ("isMeme", "bool"), ("isMargin", "bool"), ("isRwa", "bool"), ("pool", "address"), ("positionId", "uint256"),
          ("dexId", "uint8")),
    event("InstantFirstBuy", ("token", "address", True), ("buyer", "address", True), ("ethIn", "uint256"),
          ("tokensOut", "uint256")),
    dex.V3_SWAP,
)


class OdysseyInstant(V3PoolsMixin, EvmLaunchpad):
    spec = LAUNCHPADS["odyssey_instant"]
    events = INSTANT_EVENTS

    def __init__(self, rpc) -> None:
        super().__init__(rpc)
        self._pools_init()

    @property
    def factory(self) -> str:
        return self.spec.contracts["instant_factory"]

    async def _emitters(self) -> list[str] | None:
        return [self.factory, *(m["pool"] for m in self.pools.values())]

    async def _handle(self, name: str, a: dict[str, Any], log: dict[str, Any], at: datetime, res: ScanResult) -> None:
        if name == "Swap":
            await self._handle_swap(a, log, at, res)
            return
        if (log.get("address") or "").lower() != self.factory.lower():
            res.rejected_foreign += 1
            return
        if name == "InstantTokenCreated":
            self.register_pool(a["pool"], a["token"], quote=None)  # quote side read from the pool
            res.launches.append(self._launch(log, at, a["token"], a["creator"], extra={
                "pool": a["pool"], "is_meme": a["isMeme"], "is_margin": a["isMargin"], "is_rwa": a["isRwa"],
                "position_id": a["positionId"], "dex_id": a["dexId"]}))
        else:  # InstantFirstBuy: the pool's own Swap log records the trade; kept as context only
            res.other.append({"event": name, "token": a["token"], "buyer": a["buyer"], "eth_in": str(a["ethIn"]),
                              "tokens_out": str(a["tokensOut"]), "tx_hash": log.get("transactionHash")})

    async def get_token_state(self, token: str) -> TokenState:
        return await self.v3_state(token, TokenCategory.FRESH)

    async def quote_buy(self, token: str, quote_in: int) -> Quote:
        return await self.v3_quote(token, quote_in, buy=True)

    async def quote_sell(self, token: str, tokens_in: int) -> Quote:
        return await self.v3_quote(token, tokens_in, buy=False)


REFLECTION_EVENTS = EventSet(
    event("TokenCreated", ("token", "address", True), ("creator", "address", True), ("rewardToken", "address"),
          ("threshold", "uint256")),
    TRADED,
    event("PoolMigratedV4", ("token", "address", True), ("poolId", "bytes32", True), ("liquidity", "uint128"),
          ("tokenAmt", "uint256"), ("quoteAmt", "uint256")),
)


class OdysseyReflection(EvmLaunchpad):
    """Observe only: launches, curve trades and V4 migrations are recorded;
    quotes are refused (spec.supports_trading is False)."""

    spec = LAUNCHPADS["odyssey_reflection"]
    events = REFLECTION_EVENTS
    emitter_keys = ("reflection_factory",)

    async def _handle(self, name: str, a: dict[str, Any], log: dict[str, Any], at: datetime, res: ScanResult) -> None:
        if name == "TokenCreated":
            res.launches.append(self._launch(log, at, a["token"], a["creator"],
                                             extra={"reward_token": a["rewardToken"], "threshold": str(a["threshold"])}))
        elif name == "Traded":
            res.trades.append(self._trade(log, at, **_trade_kwargs(a)))
        else:
            res.migrations.append({"token": a["token"], "venue": "uniswap_v4", "pool_id": a["poolId"],
                                   "tx_hash": log.get("transactionHash"), "at": at, "tradable": False})

    async def quote_buy(self, token: str, quote_in: int) -> Quote:
        return dex.failed(quote_in, "odyssey_reflection", "observe only: trading not supported for this venue")

    async def quote_sell(self, token: str, tokens_in: int) -> Quote:
        return dex.failed(tokens_in, "odyssey_reflection", "observe only: trading not supported for this venue")
