"""HyperliquidProvider — perpetuals via POST /exchange (signed) and /info.

Signing, wire formats and request types come from the official
hyperliquid-python-sdk (pinned 0.24.0, the `hyperliquid` extra):
`sign_l1_action`, `order_request_to_order_wire`,
`order_wires_to_order_action`, `Cloid`. Transport is our own async httpx
client so calls are non-blocking, tracked in venue health and testable.

The signer is an API ("agent") wallet approved on the Hyperliquid web app
— HYPERLIQUID_API_WALLET_PRIVATE_KEY — acting for the main account
HYPERLIQUID_ACCOUNT_ADDRESS. An agent wallet can trade but cannot
withdraw. The key is held as a SecretStr, only turned into a signer in
memory, never logged or returned.

Market orders are IOC limit orders at mid ± slippage (the SDK's
market_open). A fill is never assumed: the /exchange response status is
read, and fills (price, size, fee) come from userFillsByTime for the
order id. Protection is a reduce-only trigger order (tpsl "sl",
isMarket) for the full position size.
"""

import hashlib
import time
from decimal import Decimal
from typing import Any

import httpx

from yonixalpha_core.execution.base import (
    CANCELED, FILLED, OPEN, REJECTED, UNKNOWN, ExecutionError, Fill, InstrumentRules, NotConfigured,
    OrderState, PositionInfo, dec,
)
from yonixalpha_core.venues.common import tracked

MAINNET = "https://api.hyperliquid.xyz"
TESTNET = "https://api.hyperliquid-testnet.xyz"
DEFAULT_SLIPPAGE = Decimal("0.02")
STATUS = {"open": OPEN, "filled": FILLED, "canceled": CANCELED, "triggered": OPEN, "rejected": REJECTED,
          "marginCanceled": CANCELED, "reduceOnlyCanceled": CANCELED, "selfTradeCanceled": CANCELED,
          "siblingFilledCanceled": CANCELED, "liquidatedCanceled": CANCELED, "openInterestCapCanceled": CANCELED,
          "scheduledCancel": CANCELED, "tickRejected": REJECTED, "minTradeNtlRejected": REJECTED,
          "perpMarginRejected": REJECTED, "reduceOnlyRejected": REJECTED, "badAloPxRejected": REJECTED,
          "iocCancelRejected": CANCELED, "badTriggerPxRejected": REJECTED, "marketOrderNoLiquidityRejected": REJECTED}


def cloid_for(client_id: str) -> str:
    """Hyperliquid client ids are 16 bytes hex; ours are free-form strings,
    so the cloid is a stable hash of it (same client id -> same cloid)."""
    return "0x" + hashlib.sha256(client_id.encode()).hexdigest()[:32]


def _sdk():
    try:
        from hyperliquid.utils import signing
        from hyperliquid.utils.types import Cloid
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise NotConfigured("hyperliquid-python-sdk is not installed (install yonixalpha-core[hyperliquid])") from exc
    return signing, Cloid


class HyperliquidProvider:
    venue = "hyperliquid"
    quote_currency = "USDC"

    def __init__(self, client: httpx.AsyncClient, account_address: str | None, api_wallet_key: Any = None,
                 testnet: bool = False, slippage: Decimal = DEFAULT_SLIPPAGE, clock=time.time):
        self.client = client
        self.address = (account_address or "").lower() or None
        self._key = api_wallet_key  # SecretStr | str | None
        self.base = TESTNET if testnet else MAINNET
        self.is_mainnet = not testnet
        self.slippage = slippage
        self._clock = clock
        self._wallet = None
        self._meta: dict[str, tuple[int, int]] | None = None  # coin -> (asset index, szDecimals)

    @property
    def configured(self) -> bool:
        return bool(self.address and self._secret())

    def _secret(self) -> str:
        k = self._key
        if k is None:
            return ""
        return k.get_secret_value() if hasattr(k, "get_secret_value") else str(k)

    def _signer(self):
        if not self.configured:
            raise NotConfigured("HYPERLIQUID_ACCOUNT_ADDRESS / HYPERLIQUID_API_WALLET_PRIVATE_KEY not set")
        if self._wallet is None:
            import eth_account
            try:
                self._wallet = eth_account.Account.from_key(self._secret())
            except (ValueError, TypeError):
                raise NotConfigured("HYPERLIQUID_API_WALLET_PRIVATE_KEY is not a valid private key") from None
        return self._wallet

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        async def go():
            try:
                resp = await self.client.post(f"{self.base}{path}", json=body, timeout=10.0)
            except httpx.HTTPError as exc:
                raise ExecutionError(f"hyperliquid {path}: transport {type(exc).__name__}") from exc
            try:
                data = resp.json()
            except ValueError as exc:
                raise ExecutionError(f"hyperliquid {path}: HTTP {resp.status_code} non-JSON") from exc
            if resp.status_code >= 400:
                raise ExecutionError(f"hyperliquid {path}: HTTP {resp.status_code} {str(data)[:200]}")
            return data
        return await tracked("hyperliquid", go())

    async def _info(self, body: dict[str, Any]) -> Any:
        return await self._post("/info", body)

    async def _action(self, action: dict[str, Any]) -> Any:
        signing, _ = _sdk()
        nonce = int(self._clock() * 1000)
        signature = signing.sign_l1_action(self._signer(), action, None, nonce, None, self.is_mainnet)
        data = await self._post("/exchange", {"action": action, "nonce": nonce, "signature": signature,
                                              "vaultAddress": None, "expiresAfter": None})
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise ExecutionError(f"hyperliquid {action.get('type')}: {str(data)[:200]}")
        return data.get("response") or {}

    # -- rules / setup ---------------------------------------------------------

    async def _asset(self, coin: str) -> tuple[int, int]:
        if self._meta is None:
            meta = await self._info({"type": "meta"})
            self._meta = {a["name"]: (i, int(a.get("szDecimals", 0))) for i, a in enumerate(meta.get("universe", []))}
        if coin not in self._meta:
            raise ExecutionError(f"hyperliquid: coin {coin} not listed")
        return self._meta[coin]

    async def instrument(self, symbol: str) -> InstrumentRules:
        _, sz_dec = await self._asset(symbol)
        # Perp prices: 5 significant figures and at most 6 - szDecimals decimals.
        return InstrumentRules(symbol, Decimal(1).scaleb(-sz_dec), Decimal(1).scaleb(-(6 - sz_dec)),
                               Decimal(1).scaleb(-sz_dec), Decimal(10), price_sig_figs=5)

    async def prepare(self, symbol: str, leverage: int) -> None:
        asset, _ = await self._asset(symbol)
        await self._action({"type": "updateLeverage", "asset": asset, "isCross": True, "leverage": int(leverage)})

    # -- orders ----------------------------------------------------------------

    async def _order(self, symbol: str, is_buy: bool, qty: Decimal, px: Decimal, order_type: dict, reduce_only: bool,
                     client_id: str) -> dict[str, Any]:
        signing, Cloid = _sdk()
        asset, _ = await self._asset(symbol)
        wire = signing.order_request_to_order_wire(
            {"coin": symbol, "is_buy": is_buy, "sz": float(qty), "limit_px": float(px), "order_type": order_type,
             "reduce_only": reduce_only, "cloid": Cloid(cloid_for(client_id))}, asset)
        resp = await self._action(signing.order_wires_to_order_action([wire]))
        statuses = ((resp.get("data") or {}).get("statuses")) or [{}]
        return statuses[0]

    async def market_order(self, symbol: str, side: str, qty: Decimal, reduce_only: bool, client_id: str,
                           ref_price: Decimal | None = None) -> OrderState:
        rules = await self.instrument(symbol)
        mid = ref_price
        if mid is None:
            mids = await self._info({"type": "allMids"})
            mid = dec(mids.get(symbol))
            if mid is None:
                raise ExecutionError(f"hyperliquid: no mid price for {symbol}")
        is_buy = side == "BUY"
        px = rules.round_price(mid * (1 + self.slippage if is_buy else 1 - self.slippage))
        status = await self._order(symbol, is_buy, rules.round_qty(qty), px, {"limit": {"tif": "Ioc"}}, reduce_only,
                                   client_id)
        if "error" in status:
            return OrderState(client_id=client_id, status=REJECTED, error=str(status["error"])[:300], raw=status)
        if "filled" in status:
            f = status["filled"]
            oid = str(f.get("oid"))
            fee = await self._fee_for(symbol, oid)
            return OrderState(client_id=client_id, status=FILLED, filled_qty=dec(f.get("totalSz"), Decimal(0)),
                              avg_price=dec(f.get("avgPx")), fee=fee, exchange_id=oid, raw=status)
        return await self.order_status(symbol, client_id)

    async def order_status(self, symbol: str, client_id: str) -> OrderState:
        body = await self._info({"type": "orderStatus", "user": self.address, "oid": cloid_for(client_id)})
        if not isinstance(body, dict) or body.get("status") != "order":
            return OrderState(client_id=client_id, status=REJECTED if body.get("status") == "unknownOid" else UNKNOWN,
                              error="order not known to the exchange", raw=body if isinstance(body, dict) else {})
        outer = body.get("order") or {}
        o = outer.get("order") or {}
        oid = str(o.get("oid")) if o.get("oid") is not None else None
        orig, rest = dec(o.get("origSz"), Decimal(0)), dec(o.get("sz"), Decimal(0))
        status = STATUS.get(outer.get("status"), UNKNOWN)
        filled = orig - rest
        if status == CANCELED and filled > 0 and filled >= orig:
            status = FILLED  # a cancelled IOC with a partial fill stays CANCELED (terminal) with filled_qty > 0
        avg, fee = None, Decimal(0)
        if filled > 0 and oid:
            fills = [f for f in await self.fills_since(symbol, int(outer.get("statusTimestamp", 0)) - 86_400_000)
                     if f.order_id == oid]
            qty = sum((f.qty for f in fills), Decimal(0))
            if qty > 0:
                avg = sum((f.qty * f.price for f in fills), Decimal(0)) / qty
                fee = sum((f.fee for f in fills), Decimal(0))
        return OrderState(client_id=client_id, status=status, filled_qty=filled, avg_price=avg, fee=fee,
                          exchange_id=oid, raw={"status": outer.get("status")})

    async def _fee_for(self, symbol: str, oid: str) -> Decimal:
        start = int(self._clock() * 1000) - 600_000
        return sum((f.fee for f in await self.fills_since(symbol, start) if f.order_id == oid), Decimal(0))

    # -- protection ------------------------------------------------------------

    async def set_stop(self, symbol: str, position_side: str, stop_price: Decimal, qty: Decimal, client_id: str) -> str:
        rules = await self.instrument(symbol)
        trigger = rules.round_price(stop_price)
        status = await self._order(symbol, position_side != "LONG", rules.round_qty(qty), trigger,
                                   {"trigger": {"triggerPx": float(trigger), "isMarket": True, "tpsl": "sl"}}, True,
                                   client_id)
        if "error" in status:
            raise ExecutionError(f"hyperliquid stop rejected: {str(status['error'])[:200]}")
        resting = status.get("resting") or {}
        return str(resting.get("oid") or cloid_for(client_id))

    async def _open_orders(self) -> list[dict[str, Any]]:
        rows = await self._info({"type": "frontendOpenOrders", "user": self.address})
        return rows if isinstance(rows, list) else []

    async def open_protection(self, symbol: str) -> list[dict[str, Any]]:
        return [{"id": str(o.get("oid")), "type": o.get("orderType"), "trigger_price": o.get("triggerPx"),
                 "side": "SELL" if o.get("side") == "A" else "BUY"}
                for o in await self._open_orders() if o.get("coin") == symbol and o.get("isTrigger")]

    async def cancel_stop(self, symbol: str, stop_id: str) -> None:
        if not stop_id.isdigit():
            return
        asset, _ = await self._asset(symbol)
        resp = await self._action({"type": "cancel", "cancels": [{"a": asset, "o": int(stop_id)}]})
        errors = [s for s in ((resp.get("data") or {}).get("statuses") or []) if isinstance(s, dict) and "error" in s]
        if errors:
            raise ExecutionError(f"hyperliquid cancel: {str(errors)[:200]}")

    async def cancel_protection(self, symbol: str) -> None:
        ids = [int(p["id"]) for p in await self.open_protection(symbol)]
        if not ids:
            return
        asset, _ = await self._asset(symbol)
        resp = await self._action({"type": "cancel", "cancels": [{"a": asset, "o": oid} for oid in ids]})
        errors = [s for s in ((resp.get("data") or {}).get("statuses") or []) if isinstance(s, dict) and "error" in s]
        if errors:
            raise ExecutionError(f"hyperliquid cancel: {str(errors)[:200]}")

    # -- grid (resting limit orders) ------------------------------------------

    async def limit_order(self, symbol: str, side: str, qty: Decimal, price: Decimal, client_id: str,
                          post_only: bool = True, reduce_only: bool = False) -> OrderState:
        rules = await self.instrument(symbol)
        status = await self._order(symbol, side == "BUY", rules.round_qty(qty), rules.round_price(price),
                                   {"limit": {"tif": "Alo" if post_only else "Gtc"}}, reduce_only, client_id)
        if "error" in status:
            return OrderState(client_id=client_id, status=REJECTED, error=str(status["error"])[:300], raw=status)
        if "filled" in status:
            f = status["filled"]
            return OrderState(client_id=client_id, status=FILLED, filled_qty=dec(f.get("totalSz"), Decimal(0)),
                              avg_price=dec(f.get("avgPx")), exchange_id=str(f.get("oid")), raw=status)
        return OrderState(client_id=client_id, status=OPEN, exchange_id=str((status.get("resting") or {}).get("oid")),
                          raw=status)

    async def cancel_order(self, symbol: str, client_id: str) -> None:
        asset, _ = await self._asset(symbol)
        resp = await self._action({"type": "cancelByCloid", "cancels": [{"asset": asset, "cloid": cloid_for(client_id)}]})
        errors = [s for s in ((resp.get("data") or {}).get("statuses") or []) if isinstance(s, dict) and "error" in s]
        if errors:
            raise ExecutionError(f"hyperliquid cancel: {str(errors)[:200]}")

    # -- account ---------------------------------------------------------------

    async def _state(self) -> dict[str, Any]:
        if not self.address:
            raise NotConfigured("HYPERLIQUID_ACCOUNT_ADDRESS not set")
        return await self._info({"type": "clearinghouseState", "user": self.address})

    async def position(self, symbol: str) -> PositionInfo | None:
        for ap in (await self._state()).get("assetPositions", []):
            p = ap.get("position") or {}
            size = dec(p.get("szi"), Decimal(0))
            if p.get("coin") == symbol and size != 0:
                return PositionInfo(symbol, size, dec(p.get("entryPx")), None, dec(p.get("unrealizedPnl")))
        return None

    async def balance(self) -> Decimal:
        return dec((await self._state()).get("withdrawable"), Decimal(0))

    async def fills_since(self, symbol: str, start_ms: int) -> list[Fill]:
        if not self.address:
            raise NotConfigured("HYPERLIQUID_ACCOUNT_ADDRESS not set")
        rows = await self._info({"type": "userFillsByTime", "user": self.address, "startTime": max(0, start_ms)})
        return [Fill(str(r.get("oid")), r.get("cloid"), "BUY" if r.get("side") == "B" else "SELL",
                     dec(r.get("sz"), Decimal(0)), dec(r.get("px"), Decimal(0)), dec(r.get("fee"), Decimal(0)),
                     dec(r.get("closedPnl")), int(r.get("time", 0)))
                for r in rows or [] if r.get("coin") == symbol]

